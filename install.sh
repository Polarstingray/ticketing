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

# Set KEY=VALUE in .env (replace the line if present, else append).
set_env() {
  key="$1"; val="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    tmp="$(mktemp)"
    grep -v "^${key}=" .env >"$tmp"
    mv "$tmp" .env
  fi
  printf '%s=%s\n' "$key" "$val" >>.env
}

# Same, against resolver/.env (which must already exist).
set_resolver_env() {
  key="$1"; val="$2"
  if grep -q "^${key}=" resolver/.env 2>/dev/null; then
    tmp="$(mktemp)"
    grep -v "^${key}=" resolver/.env >"$tmp" || true
    mv "$tmp" resolver/.env
  fi
  printf '%s=%s\n' "$key" "$val" >>resolver/.env
}

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
    DBOOT="$($DC exec -T backend cat /data/digest-bootstrap.json 2>/dev/null || true)"
    if [ -z "$DBOOT" ]; then
      echo "    No digest bootstrap file found. The key is minted only when"
      echo "    SEED_DIGEST_BOT=true on boot. Set it in .env and re-run ./install.sh,"
      echo "    or mint an admin key from Profile → API keys and put it in"
      echo "    resolver/.env as DIGEST_ADMIN_KEY. Skipping digest setup."
    elif [ ! -f resolver/.env ]; then
      echo "    resolver/.env does not exist yet. Set up the resolver first (above),"
      echo "    then re-run ./install.sh to add the digest key. Skipping digest setup."
    else
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
