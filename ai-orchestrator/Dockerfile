FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 6000
ENV UVICORN_WORKERS=4
ENV AI_MODEL_BACKEND=ollama
ENV OLLAMA_WILE_URL=http://192.168.1.50:11434
ENV OLLAMA_ROADRUNNER_URL=http://192.168.1.51:11434
ENV MAX_LLM_CONCURRENCY=20
ENV MAX_OLLAMA_PER_NODE=4
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6000", "--workers", "4", "--limit-concurrency", "200", "--timeout-keep-alive", "30"]