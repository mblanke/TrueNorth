FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime, and they carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel

COPY . .
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app

EXPOSE 8080

# Multi-worker uvicorn (4 workers for 1200-user scale)
# Workers = 2 * CPU_CORES + 1, tunable via UVICORN_WORKERS env
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${UVICORN_WORKERS:-4} --limit-concurrency 500 --timeout-keep-alive 30 --access-log"]