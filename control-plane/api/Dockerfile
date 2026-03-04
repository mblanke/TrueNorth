FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Multi-worker uvicorn (4 workers for 1200-user scale)
# Workers = 2 * CPU_CORES + 1, tunable via UVICORN_WORKERS env
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${UVICORN_WORKERS:-4} --limit-concurrency 500 --timeout-keep-alive 30 --access-log"]