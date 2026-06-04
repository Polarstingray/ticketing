FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; bcrypt ships wheels so no build toolchain needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Database lives on a mounted volume at /data.
ENV DATABASE_PATH=/data/stingray.db
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
