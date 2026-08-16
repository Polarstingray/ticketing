FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; bcrypt ships wheels so no build toolchain needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Database lives on a mounted volume at /data.
ENV DATABASE_PATH=/data/stingray.db
EXPOSE 8000

# --proxy-headers makes the backend trust X-Forwarded-For / X-Real-IP set by the
# nginx frontend, so request.client.host (and thus per-IP rate limiting) reflects
# the real client instead of the nginx container IP. --forwarded-allow-ips "*"
# rests on TWO invariants — break either and every per-IP limit becomes
# client-controlled:
#   1. The backend is never published; it is reachable only from the internal
#      Docker network via nginx (see docker-compose.yml).
#   2. nginx OVERWRITES X-Forwarded-For with $remote_addr rather than appending
#      to the client's value ($proxy_add_x_forwarded_for). "*" makes uvicorn take
#      the leftmost entry, which with append semantics is whatever the caller
#      sent. See frontend/nginx.conf and deploy/demo/nginx.conf.
#
# Also note: no --workers here on purpose. backend/login_throttle.py and the
# slowapi limiter keep per-process in-memory counters, so a second worker would
# multiply every limit by the worker count.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
