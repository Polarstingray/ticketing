#!/usr/bin/env bash
#
# Install (or remove) the auto-deploy git hooks.
#
# The hooks themselves live in deploy/hooks/ so they are tracked, reviewable and
# diffable; .git/hooks is not part of the repository and never survives a clone.
# What lands in .git/hooks is a one-line shim that execs the tracked file, so
# editing the real hook is an ordinary tracked change.
#
# This copies shims rather than setting core.hooksPath, which would take over
# hook resolution for the whole repo and silently disable anything already there.
#
#   deploy/install-hooks.sh            # install
#   deploy/install-hooks.sh --uninstall
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_DIR="$(git -C "$REPO_ROOT" rev-parse --git-path hooks)"
HOOK_DIR="$(cd "$REPO_ROOT" && cd "$HOOK_DIR" && pwd)"
HOOKS=(post-commit post-merge)
MARKER="# stingray-autodeploy"

if [[ "${1:-}" == "--uninstall" ]]; then
  for hook in "${HOOKS[@]}"; do
    target="$HOOK_DIR/$hook"
    if [[ -f "$target" ]] && grep -q "$MARKER" "$target"; then
      rm -f "$target"
      echo "removed $target"
    else
      echo "skipped $target (not ours)"
    fi
  done
  exit 0
fi

for hook in "${HOOKS[@]}"; do
  target="$HOOK_DIR/$hook"
  # Never clobber a hook somebody else wrote.
  if [[ -f "$target" ]] && ! grep -q "$MARKER" "$target"; then
    echo "WARNING: $target exists and is not ours — leaving it alone." >&2
    echo "         Add this line to it by hand to chain the deploy:" >&2
    echo "         exec \"\$(git rev-parse --show-toplevel)/deploy/hooks/$hook\"" >&2
    continue
  fi
  cat >"$target" <<EOF
#!/usr/bin/env bash
$MARKER — thin shim; the real hook is tracked at deploy/hooks/$hook
exec "\$(git rev-parse --show-toplevel)/deploy/hooks/$hook" "\$@"
EOF
  chmod +x "$target"
  echo "installed $target -> deploy/hooks/$hook"
done

cat <<'EOF'

Auto-deploy is armed. On a commit or merge to main that touches backend/,
frontend/ or docker-compose.yml, the stack is rebuilt in the background once
both test suites pass.

  tail -f deploy/.autodeploy.log     watch a deploy
  touch deploy/.autodeploy-disabled  pause it (delete the file to resume)
  make deploy                        deploy now, from any branch
EOF
