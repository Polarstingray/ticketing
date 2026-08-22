#!/usr/bin/env sh
# Stingray Tickets — one-command installer.
#
# Brings up the core app (backend + frontend) with safe generated secrets, and
# optionally provisions the resolver bot and writes resolver/.env for you. Safe to
# re-run: it won't clobber an existing .env, and seeding is idempotent.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# --- prerequisites --------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: Docker with Compose is required. Install Docker Desktop or the docker-compose plugin." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required (used to generate the session secret)." >&2
  exit 1
fi

# Set KEY=VALUE in an env file (replace the line if present, else append).
#
# Matching is exact, not a regex: awk's index($0, "KEY=") == 1 tests a literal
# prefix, so a key containing regex metacharacters can't match a neighbouring
# line the way `grep "^${key}="` would. The rewrite is also done in place (cat
# back into the original file) rather than `mv`ing a mktemp over it — these files
# hold live API keys, and mv would replace the file's mode and ownership with
# mktemp's.
set_env_in() {
  _file="$1"; _key="$2"; _val="$3"
  if [ ! -e "$_file" ]; then
    (umask 077; : >"$_file")   # holds credentials — never world-readable
  fi
  _tmp="$(mktemp)"
  awk -v k="${_key}=" 'index($0, k) != 1' "$_file" >"$_tmp"
  cat "$_tmp" >"$_file"        # truncate in place: keeps mode/owner
  rm -f "$_tmp"
  printf '%s=%s\n' "$_key" "$_val" >>"$_file"
}

set_env() { set_env_in .env "$1" "$2"; }

# Same, against resolver/.env (which must already exist).
set_resolver_env() { set_env_in resolver/.env "$1" "$2"; }

prompt() {  # prompt VAR "question" "default"
  _q="$2"; _def="$3"
  printf '%s [%s]: ' "$_q" "$_def" >&2
  read -r _ans || _ans=""
  [ -n "$_ans" ] || _ans="$_def"
  eval "$1=\$_ans"
}

# --- .env bootstrap -------------------------------------------------------
if [ -f .env ]; then
  echo "==> Using existing .env (leaving it untouched)."
else
  echo "==> Creating .env from .env.example with generated secrets."
  cp .env.example .env
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  set_env SESSION_SECRET "$SECRET"
  prompt ADMIN_PW "Admin password for the first login" "changeme-please"
  set_env ADMIN_PASSWORD "$ADMIN_PW"
  set_env SEED_RESOLVER_BOT "true"
  set_env SEED_DIGEST_BOT "true"
  echo "    Wrote .env (SESSION_SECRET generated, admin password set)."
fi

# --- bring up the backend and wait for health -----------------------------
echo "==> Building and starting the backend..."
$DC up -d --build backend

printf "==> Waiting for the backend to become healthy"
i=0
until $DC exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo " timed out." >&2
    echo "Check logs with: $DC logs backend" >&2
    exit 1
  fi
  printf "."
  sleep 2
done
echo " ok."

# --- optional: resolver bot + resolver/.env -------------------------------
printf "==> Set up the optional AI resolver now? It needs an agent CLI (Claude Code or opencode) + provider API keys. [y/N]: "
read -r WANT_RESOLVER || WANT_RESOLVER="n"
case "$WANT_RESOLVER" in
  y | Y | yes | YES)
    BOOT="$($DC exec -T backend cat /data/resolver-bootstrap.json 2>/dev/null || true)"
    if [ -z "$BOOT" ]; then
      echo "    No bootstrap file found. The bot is seeded only when SEED_RESOLVER_BOT=true"
      echo "    on first boot (empty DB). If the DB already existed, create the bot user"
      echo "    manually — see resolver/README.md. Skipping resolver setup."
    else
      BOT_ID="$(printf '%s' "$BOOT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')"
      BOT_KEY="$(printf '%s' "$BOOT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
      if [ -f resolver/.env ]; then
        echo "    resolver/.env already exists — leaving it untouched."
      else
        cp resolver/.env.example resolver/.env
        prompt PROJ "Absolute path to the directory holding repos the resolver may touch" "$HOME/projects"
        set_resolver_env STINGRAY_URL "http://localhost:8000"
        set_resolver_env STINGRAY_API_KEY "$BOT_KEY"
        set_resolver_env RESOLVER_BOT_USER_ID "$BOT_ID"
        set_resolver_env PROJECTS_ROOT "$PROJ"
        echo "    Wrote resolver/.env (bot id=$BOT_ID, key filled in)."
        echo "    Next: cd resolver && ./setup.sh   (creates the venv; then schedule resolve_tickets.py)."
      fi
    fi
    ;;
  *)
    echo "    Skipping resolver setup. You can run ./install.sh again later, or see resolver/README.md."
    ;;
esac

# --- optional: daily digest (admin key + digests.toml) --------------------
# The digest is a separate scheduled job from the resolver, but it lives in the
# same directory and reads the same resolver/.env — so all this does is fill in
# DIGEST_ADMIN_KEY and lay down a config to edit.
printf "==> Set up the optional daily digest now? It files a backlog report ticket on a schedule. [y/N]: "
read -r WANT_DIGEST || WANT_DIGEST="n"
case "$WANT_DIGEST" in
  y | Y | yes | YES)
    if [ ! -f resolver/.env ]; then
      echo "    resolver/.env does not exist yet. Set up the resolver first (above),"
      echo "    then re-run ./install.sh to add the digest key. Skipping digest setup."
    else
      # Three outcomes, kept apart on purpose: got the file (0), the backend is
      # up but the file isn't there (3), or the exec itself failed (anything
      # else) — which is an operator problem, not a "digest wasn't seeded" one.
      if DBOOT="$($DC exec -T backend sh -c 'cat /data/digest-bootstrap.json 2>/dev/null || exit 3' 2>/dev/null)"; then
        DRC=0
      else
        DRC=$?
      fi
      if [ "$DRC" -eq 0 ] && [ -n "$DBOOT" ]; then
        DIGEST_KEY="$(printf '%s' "$DBOOT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
        set_resolver_env DIGEST_ADMIN_KEY "$DIGEST_KEY"
        echo "    Set DIGEST_ADMIN_KEY in resolver/.env (an admin key — the digest must see every ticket)."
        if [ -f resolver/digests.toml ]; then
          echo "    resolver/digests.toml already exists — leaving it untouched."
        else
          cp resolver/digests.example.toml resolver/digests.toml
          echo "    Copied digests.example.toml → resolver/digests.toml (edit assign_to and the query)."
        fi
        echo "    Try it with: cd resolver && ./digest.py --name daily --dry-run"
        echo "    Then schedule it — see resolver/README.md → 'Daily digest'."
      elif [ "$DRC" -eq 3 ]; then
        echo "    No digest bootstrap file in the backend container. The key is minted"
        echo "    only when SEED_DIGEST_BOT=true on a boot where the admin has no"
        echo "    'digest' key yet. Set it in .env and re-run ./install.sh, or mint an"
        echo "    admin key from Profile → API keys and put it in resolver/.env as"
        echo "    DIGEST_ADMIN_KEY. Skipping digest setup."
      else
        echo "    Could not read the digest bootstrap file: '$DC exec backend' failed"
        echo "    (exit $DRC). The backend container is probably not running — check"
        echo "    '$DC ps' and '$DC logs backend', then re-run ./install.sh."
        echo "    Skipping digest setup."
      fi
    fi
    ;;
  *)
    echo "    Skipping digest setup. You can run ./install.sh again later, or see resolver/README.md."
    ;;
esac

# --- bring up the frontend ------------------------------------------------
echo "==> Building and starting the frontend..."
$DC up -d --build frontend

echo ""
echo "✅ Stingray is up:  http://localhost:3000"
echo "   Log in as the admin user from your .env (ADMIN_USERNAME / ADMIN_PASSWORD)."
echo "   The first admin API key is printed once in the backend log: $DC logs backend | grep '\\[seed\\]'"
