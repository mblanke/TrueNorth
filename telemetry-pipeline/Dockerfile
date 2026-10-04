FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app
CMD ["python", "-m", "pipeline.ingest"]