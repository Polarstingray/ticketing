#!/usr/bin/env sh
# Resolver setup: build the venv and sanity-check .env.
#
# The resolver is an OPTIONAL, advanced add-on: it drives a coding-agent CLI
# (Claude Code or opencode) against your repos and opens PRs, so it needs that
# CLI installed and your provider API keys configured (your cost). The core
# ticketing app does not need any of this.
#
# install.sh (at the repo root) already writes resolver/.env for you when you opt
# in. Run this script from the resolver/ directory afterwards.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi

# --- venv -----------------------------------------------------------------
if [ ! -d .venv ]; then
  echo "==> Creating .venv (via ensurepip)..."
  python3 -m venv .venv || {
    # Some minimal environments lack the venv seed; fall back to ensurepip.
    python3 -m venv --without-pip .venv
    .venv/bin/python -m ensurepip --upgrade
  }
fi
echo "==> Installing dependencies..."
.venv/bin/python -m pip install -q -r requirements.txt

# --- .env check -----------------------------------------------------------
if [ ! -f .env ]; then
  echo "" >&2
  echo "WARNING: resolver/.env not found. Either run the root ./install.sh (recommended)" >&2
  echo "or 'cp .env.example .env' and fill in STINGRAY_URL, STINGRAY_API_KEY," >&2
  echo "RESOLVER_BOT_USER_ID and PROJECTS_ROOT." >&2
  exit 1
fi

missing=""
for key in STINGRAY_URL STINGRAY_API_KEY PROJECTS_ROOT; do
  val="$(grep "^${key}=" .env | head -n1 | cut -d= -f2-)"
  case "$key:$val" in
    STINGRAY_API_KEY:sk_replace_me | *:) missing="$missing $key" ;;
  esac
done
if [ -n "$missing" ]; then
  echo "WARNING: these resolver/.env values still need setting:$missing" >&2
fi

echo ""
echo "✅ Resolver ready. Do a dry run:"
echo "   .venv/bin/python resolve_tickets.py --dry-run"
echo "Then schedule it (see resolver/README.md for cron / the systemd template)."
