#!/usr/bin/env bash
#
# Rebuild and restart the local Stingray stack, gated on the test suites.
#
# Invoked two ways:
#   * by the git post-commit / post-merge hooks (see deploy/hooks/, installed by
#     deploy/install-hooks.sh), which detach it so `git commit` returns at once;
#   * by hand, via `make deploy`.
#
# Deliberate design points:
#
#   * Only `main` deploys. A feature branch is work in progress by definition; the
#     moment a feature is "done" is the moment it lands on the default branch.
#     This gate is for the hooks only — `make deploy` deploys whatever branch you
#     are on, on purpose. Override with DEPLOY_BRANCH= (empty means "any branch").
#   * Tests gate the swap. If anything is red we log why and leave the running
#     containers alone, so the box keeps serving the last good build rather than
#     going down with it.
#   * One at a time. A flock means two commits in quick succession queue instead
#     of racing two `docker compose up --build` runs at the same volume.
#   * The build context is the *working tree*, not the commit, so a dirty tree
#     deploys uncommitted code. That is worth knowing rather than preventing —
#     it is also what makes `make deploy` useful mid-change — so it is logged
#     loudly instead of blocked.
#   * Everything runs inside main(), called on the last line. Bash reads a script
#     incrementally as it executes it, so editing this file while a deploy is in
#     flight shifts the byte offset under the running shell and it resumes
#     mid-token — observed here as "unexpected EOF while looking for matching
#     quote" on a file that was perfectly valid. Wrapping the body in a function
#     forces bash to parse to the closing brace before it executes anything, so
#     by the time the work starts the whole file has been read. The trailing call
#     and exit share one line for the same reason: nothing is read after it.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LOG="${DEPLOY_LOG:-$REPO_ROOT/deploy/.autodeploy.log}"
LOCK="$REPO_ROOT/deploy/.autodeploy.lock"
DEPLOY_BRANCH="${DEPLOY_BRANCH-main}"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://localhost:3000/api/health}"
# Paths whose contents actually end up in an image. A commit touching only docs,
# the CLI or the resolver changes nothing that is served, so it should not cost
# a two-minute rebuild.
DEPLOYABLE_RE='^(backend|frontend)/|^docker-compose\.yml$'

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

die() { log "$*"; exit 1; }

# Every run appends full pytest/vitest/docker output, and this fires on every
# commit to main indefinitely — so cap it. One generation of history is kept,
# which is all you need to compare a failed deploy against the last good one.
rotate_log() {
  local max=$((5 * 1024 * 1024))
  if [[ -f "$LOG" ]] && (( $(stat -c %s "$LOG" 2>/dev/null || echo 0) > max )); then
    mv -f "$LOG" "$LOG.1"
  fi
}

# Bring the Python venvs back in line with the requirements files the checkout now
# has. This exists because of a real outage: `resolver/requirements.txt` gained
# `-e ../cli` (the shared client package), a pull brought the code that imports it,
# and nothing reinstalled — so every cron tick died at import with
# ModuleNotFoundError. It looked exactly like "the resolver isn't scheduled", and the
# evidence was buried in an append-only cron log.
#
# Note this must run BEFORE the deployable-paths gate below: that gate skips any
# commit touching only resolver/ or cli/, which is precisely the commit that changes
# these requirements. It also runs before the auto-deploy kill switch, so a
# resolver-only box (deploy/.autodeploy-disabled, no docker) still gets its venvs
# synced on pull.
#
# Keyed on a hash of the requirements file so an ordinary pull costs nothing. A
# missing stamp counts as changed, which is what heals a venv that drifted before
# this existed.
sync_venv() {
  local dir="$1" venv="$REPO_ROOT/$1/.venv" reqs="$REPO_ROOT/$1/requirements.txt"
  [[ -f "$reqs" ]] || return 0
  # Never build a venv that was never there: a checkout may deliberately not run
  # this component, and silently creating one hides that.
  if [[ ! -x "$venv/bin/pip" ]]; then
    log "  venv-sync: $dir/.venv absent — skipping (run resolver/setup.sh to create it)"
    return 0
  fi

  local stamp="$venv/.requirements-sha" want have=""
  want="$(sha256sum "$reqs" | cut -d' ' -f1)"
  [[ -f "$stamp" ]] && have="$(cat "$stamp" 2>/dev/null)"
  if [[ "$want" == "$have" ]]; then
    return 0
  fi

  # cd into the component: requirements may use paths relative to it (`-e ../cli`),
  # which pip resolves against the working directory, not the requirements file.
  log "  venv-sync: $dir/requirements.txt changed — installing…"
  if (cd "$REPO_ROOT/$dir" && "$venv/bin/pip" install -q -r requirements.txt) >>"$LOG" 2>&1; then
    printf '%s\n' "$want" >"$stamp"
    log "  venv-sync: $dir OK"
  else
    # Deliberately not fatal. A failed sync must not also block a deploy that would
    # otherwise be fine, and leaving the stamp unwritten means the next pull retries.
    log "  venv-sync: WARN $dir install failed — see $LOG"
  fi
}

sync_venvs() {
  # Serialized separately from the deploy lock, which is taken later and is skipped
  # entirely when auto-deploy is disabled.
  exec 8>"$REPO_ROOT/deploy/.venv-sync.lock"
  flock -w 300 8 || { log "SKIP venv-sync: lock busy"; return 0; }
  local d
  for d in backend resolver; do
    sync_venv "$d"
  done
  flock -u 8
}

main() {
  cd "$REPO_ROOT" || exit 1

  local trigger="${1:-manual}"

  # Keeping the venvs in step with the checkout is not a deploy, so it happens
  # before every gate below — including the kill switch, so a resolver-only box
  # with auto-deploy disabled still gets synced on pull. See sync_venvs.
  rotate_log
  sync_venvs

  # `make venv-sync`: do the sync above and nothing else. Useful on a box where
  # auto-deploy is off, and for healing a venv without waiting for a pull.
  if [[ "$trigger" == "venv-sync-only" ]]; then
    return 0
  fi

  # --- Kill switch -----------------------------------------------------------
  # post-commit ignores `git commit --no-verify`, so there has to be a real way to
  # turn this off without uninstalling the hook.
  if [[ "${STINGRAY_AUTODEPLOY:-1}" == "0" || -e "$REPO_ROOT/deploy/.autodeploy-disabled" ]]; then
    log "SKIP: auto-deploy disabled (STINGRAY_AUTODEPLOY=0 or deploy/.autodeploy-disabled)"
    return 0
  fi

  # --- Serialize -------------------------------------------------------------
  exec 9>"$LOCK"
  if ! flock -w 900 9; then
    die "SKIP [$trigger]: another deploy held the lock for 15m; giving up"
  fi

  local branch sha
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

  # Branch gate applies to hook-triggered runs only. `make deploy` is the manual
  # escape hatch — deliberately deploying a feature branch to the box is a normal
  # thing to want, and having the guard silently veto it would make the command
  # look broken.
  if [[ "$trigger" != "manual" && -n "$DEPLOY_BRANCH" && "$branch" != "$DEPLOY_BRANCH" ]]; then
    log "SKIP [$trigger]: on '$branch', only '$DEPLOY_BRANCH' auto-deploys"
    return 0
  fi

  # --- Does this change anything that is actually served? --------------------
  # Skipped for a manual run: if you typed `make deploy` you meant it, whatever
  # the last commit happened to touch.
  if [[ "$trigger" != "manual" ]]; then
    local range changed
    case "$trigger" in
      post-merge) range="$(git rev-parse ORIG_HEAD 2>/dev/null)..HEAD" ;;
      *)          range="" ;;
    esac
    if [[ -n "$range" && "$range" != "..HEAD" ]]; then
      changed="$(git diff --name-only "$range" 2>/dev/null)"
    else
      changed="$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)"
    fi
    if ! grep -qE "$DEPLOYABLE_RE" <<<"$changed"; then
      log "SKIP [$trigger] $branch@$sha: nothing deployable changed"
      return 0
    fi
  fi

  log "START [$trigger] $branch@$sha"
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    log "  NOTE: working tree is dirty — deploying the tree on disk, not $sha"
  fi

  # --- Gate: test suites -----------------------------------------------------
  # Prefer the project venv; `python` is frequently not on PATH even where
  # `python3` is, and a bare `python -m pytest` would fail as a missing command
  # rather than as a red suite — silently blocking every deploy.
  local py="$REPO_ROOT/backend/.venv/bin/python"
  [[ -x "$py" ]] || py="$(command -v python3 || command -v python)"
  if [[ -z "$py" ]]; then
    die "ABORT: no python interpreter found for the backend suite"
  fi

  log "  running backend tests…"
  if ! (cd backend && "$py" -m pytest -q) >>"$LOG" 2>&1; then
    die "ABORT: backend tests failed — keeping the running build. See $LOG"
  fi

  # Call the vitest binary directly rather than through `npx`. Two reasons, neither
  # of them urgent: `npm exec` adds a package-resolution step on top of a binary
  # that is already in node_modules, and a missing install surfaces here as an
  # explicit error instead of npx silently trying to fetch vitest from the registry.
  # Note the suite legitimately takes ~5 minutes of wall time on this machine
  # (vitest self-reports ~30s; the rest is module resolution), so a long-quiet
  # frontend stage in the log is expected, not a hang.
  log "  running frontend tests…"
  local vitest="$REPO_ROOT/frontend/node_modules/.bin/vitest"
  if [[ ! -x "$vitest" ]]; then
    die "ABORT: $vitest missing — run 'npm install' in frontend/ first"
  fi
  if ! (cd frontend && "$vitest" run --reporter=dot) >>"$LOG" 2>&1; then
    die "ABORT: frontend tests failed — keeping the running build. See $LOG"
  fi

  # --- Build & swap ----------------------------------------------------------
  log "  building images and restarting…"
  if ! docker compose up -d --build >>"$LOG" 2>&1; then
    die "ABORT: docker compose build/up failed. See $LOG"
  fi

  # --- Verify it actually came back ------------------------------------------
  # `up -d` returning 0 only means the containers started, not that the app serves.
  local i
  for ((i = 0; i < 30; i++)); do
    if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
      log "OK [$trigger] $branch@$sha deployed and healthy at $HEALTH_URL"
      return 0
    fi
    sleep 2
  done

  die "WARN: deployed $branch@$sha but $HEALTH_URL did not respond within 60s"
}

main "$@"; exit $?
