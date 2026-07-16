#!/bin/bash
# Boot the demo: seed illustrative data, start uvicorn, then serve via nginx.
#
# The demo DB is deliberately EPHEMERAL (no volume mounted). Every restart gets a
# clean, identical dataset — which doubles as the reset mechanism for a public
# instance anyone can click around in.
set -euo pipefail

: "${DATABASE_PATH:=/data/demo.db}"
mkdir -p "$(dirname "$DATABASE_PATH")"

# A throwaway demo has no secret worth persisting. If the host didn't inject one
# (e.g. `fly secrets set SESSION_SECRET=...`), mint one per boot — APP_ENV=production
# refuses to start on a default/known value, and sessions simply don't outlive a
# restart (neither does the data).
if [ -z "${SESSION_SECRET:-}" ]; then
  SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  export SESSION_SECRET
  echo "[demo] No SESSION_SECRET provided; generated an ephemeral one for this boot."
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

# If either process dies the container should die too, so the host restarts it
# (rather than leaving a half-up demo serving errors).
trap 'kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true' TERM INT
wait -n "$UVICORN_PID" "$NGINX_PID"
echo "[demo] A process exited; shutting down." >&2
kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
exit 1
