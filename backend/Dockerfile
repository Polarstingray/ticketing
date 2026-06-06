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
# the real client instead of the nginx container IP. --forwarded-allow-ips "*" is
# safe because the backend is only reachable from the internal Docker network via
# nginx and is never published publicly.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
