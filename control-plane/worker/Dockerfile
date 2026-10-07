FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Detection scoring (worker/detection.py) runs scenario_engine's ScoringEngine. The engine
# lives outside this build context, so it arrives as a named context and only the package
# is copied:  docker build --build-context scenario_engine=scenario-engine control-plane/worker
# (compose: additional_contexts; CI: build-contexts). Its runtime needs (httpx, pyyaml)
# are already in requirements.txt.
COPY --from=scenario_engine scenario_engine /app/scenario_engine
COPY . .
CMD ["celery","-A","worker.celery_app","worker","--loglevel=INFO"]
