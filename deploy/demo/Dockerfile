# Single-container image for the public demo.
#
# The normal deployment (../../docker-compose.yml) runs uvicorn and nginx as two
# services on an internal network. Hosts like Fly.io deploy one image per app, so
# the demo collapses both into one container: nginx serves the built SPA on :3000
# and proxies /api to uvicorn on loopback:8000.
#
# Build context is the REPO ROOT (it needs both backend/ and frontend/):
#   docker build -f deploy/demo/Dockerfile -t stingray-demo .

# --- Stage 1: build the SPA -------------------------------------------------
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: runtime (uvicorn + nginx) -------------------------------------
FROM python:3.12-slim

# nginx from the distro; bcrypt et al. ship wheels, so no build toolchain needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./

COPY --from=web /web/dist /usr/share/nginx/html
COPY deploy/demo/nginx.conf /etc/nginx/conf.d/demo.conf
COPY deploy/demo/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# APP_ENV=production makes the app refuse to boot on a default SESSION_SECRET
# (see backend/startup.py); the entrypoint mints a random one when none is set.
# The demo's login is public by design (the data is throwaway and repainted on
# every boot) — hence a "weak" password here is deliberate, not an oversight.
# READ_ONLY blocks every write (see backend/read_only_guard.py) so a stranger
# with that login can't touch ticket data, another visitor's work, or the
# admin account itself; SHOW_DEMO_CREDENTIALS is the separate, explicit opt-in
# that puts ADMIN_USERNAME/PASSWORD on the login page — never turn either of
# these on for a real deployment.
#
# CHAT_API_KEY is NOT set here: it is a real credential, and anything in this
# file is public (committed to the repo). It is provided at deploy time via
# `fly secrets set CHAT_API_KEY=...` — see deploy/demo/fly.toml's header.
ENV DATABASE_PATH=/data/demo.db \
    APP_ENV=production \
    COOKIE_SECURE=true \
    DEMO_RESET=true \
    ADMIN_USERNAME=admin \
    ADMIN_PASSWORD=demopass123 \
    READ_ONLY=true \
    SHOW_DEMO_CREDENTIALS=true \
    CHAT_API_URL=https://openrouter.ai/api/v1/chat/completions \
    CHAT_API_MODEL=openai/gpt-4o-mini \
    CHAT_PRICE_IN=0.15 \
    CHAT_PRICE_OUT=0.60 \
    CHAT_DAILY_USD_LIMIT=2.00 \
    CHAT_RATE_LIMIT=5/minute \
    PYTHONUNBUFFERED=1

EXPOSE 3000
CMD ["/entrypoint.sh"]
