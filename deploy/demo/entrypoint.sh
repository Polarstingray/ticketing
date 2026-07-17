#!/bin/bash
# Boot the demo: seed illustrative data, start uvicorn, then serve via nginx.
#
# The demo DB is deliberately EPHEMERAL (no volume mounted). Every restart gets a
# clean, identical dataset — which doubles as the reset mechanism for a public
# instance anyone can click around in.
set -euo pipefail

: "${DATABASE_PATH:=/data/demo.db}"
mkdir -p "$(dirname "$DATABASE_PATH")"

# Session cookies are signed with SESSION_SECRET, so it must be identical across
# every instance and stable across restarts: a cookie signed with one value is
# unverifiable with another, and the request silently falls back to "logged out".
#
# For a single local container a per-boot random value is fine and convenient. On
# a real host it is a trap — several machines each mint their own, and a browser
# round-robining between them appears to randomly log itself out. So on Fly we
# refuse to boot rather than serve that bug.
if [ -z "${SESSION_SECRET:-}" ]; then
  if [ -n "${FLY_APP_NAME:-}" ]; then
    cat >&2 <<MSG
[demo] FATAL: SESSION_SECRET is not set.

Session cookies are signed with it. Left unset, every machine would sign with a
different key — a cookie minted by one machine 401s on the next, so the app
appears to log users out at random — and every restart would invalidate all
sessions issued before it.

Set a persistent secret (this restarts the app automatically):

  fly secrets set SESSION_SECRET="\$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" --app $FLY_APP_NAME

Then pin the demo to ONE machine. Each machine keeps its own ephemeral SQLite
database, so two of them serve two different datasets — a ticket created on one
is missing from the other:

  fly scale count 1 --app $FLY_APP_NAME
MSG
    exit 1
  fi
  SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  export SESSION_SECRET
  echo "[demo] No SESSION_SECRET set; generated an ephemeral one for this boot."
  echo "[demo] Fine for one local container; sessions won't survive a restart."
fi

# Repaint the illustrative dataset (--force wipes whatever was there). Set
# DEMO_RESET=false to keep an existing DB across restarts.
if [ "${DEMO_RESET:-true}" = "true" ]; then
  echo "[demo] Seeding demo data into $DATABASE_PATH"
  python -m seed_demo --force
fi

# --proxy-headers so the backend trusts the X-Forwarded-* set by the nginx in
# this container (real client IP for rate limiting, scheme for Secure cookies).
uvicorn main:app \
  --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips '*' &
UVICORN_PID=$!

# Don't let nginx serve 502s while the backend is still coming up.
for _ in $(seq 1 30); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" 2>/dev/null; then
    echo "[demo] Backend is up."
    break
  fi
  kill -0 "$UVICORN_PID" 2>/dev/null || { echo "[demo] Backend exited during startup." >&2; exit 1; }
  sleep 1
done

nginx -g 'daemon off;' &
NGINX_PID=$!

# Exit status is load-bearing here. Fly stops an idle machine by sending SIGTERM,
# and the machine's restart policy is `on-failure` — so exiting non-zero on a
# *requested* shutdown looks like a crash, flyd boots the machine straight back
# up, and scale-to-zero never happens (billing the demo as if it ran 24/7).
#
# So: a signalled shutdown exits 0. Only a process dying on its own is a failure
# worth restarting for.
shutting_down=0
on_term() {
  shutting_down=1
  kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
}
trap on_term TERM INT

rc=0
wait -n "$UVICORN_PID" "$NGINX_PID" || rc=$?

if [ "$shutting_down" = "1" ]; then
  echo "[demo] Shutdown signal received; stopping cleanly."
  wait 2>/dev/null || true
  exit 0
fi

echo "[demo] A process exited unexpectedly (rc=$rc); shutting down." >&2
kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
exit 1
