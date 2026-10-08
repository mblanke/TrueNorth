FROM python:3.11-slim
WORKDIR /app
# Distro security updates at build time (release.yml blocks on fixable HIGH/CRITICAL).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime, and they carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel
# Allowlist: the app package only (2026-10 audit P0-7).
COPY app ./app
ARG TN_VERSION=dev
ENV TN_VERSION=${TN_VERSION}
LABEL org.opencontainers.image.title="truenorth-ai-orchestrator" \
      org.opencontainers.image.version="${TN_VERSION}" \
      org.opencontainers.image.source="https://github.com/mblanke/TrueNorth"
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
USER app
EXPOSE 6000
ENV UVICORN_WORKERS=4
# The live backend is an OpenAI-compatible endpoint (vLLM behind LiteLLM), not
# Ollama. These defaults used to be `ollama` plus two hardcoded 192.168.1.5x
# hosts that exist on no current network; the image only worked because compose
# overrode all three. Run standalone and it silently pointed at nothing.
ENV AI_MODEL_BACKEND=openai
ENV MAX_LLM_CONCURRENCY=20
# Still read by app/main.py:133 as the per-node connection/semaphore limit.
ENV MAX_OLLAMA_PER_NODE=4
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6000", "--workers", "4", "--limit-concurrency", "200", "--timeout-keep-alive", "30"]