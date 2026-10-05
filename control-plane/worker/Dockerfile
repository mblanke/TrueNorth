FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# setuptools and wheel are build tooling the base image ships; nothing imports them at
# runtime (the OTel 0.65b0 instrumentation no longer needs pkg_resources), and they
# carry CVEs (Trivy).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y setuptools wheel
# Detection scoring (worker/detection.py) runs scenario_engine's ScoringEngine. The engine
# lives outside this build context, so it arrives as a named context and only the package
# is copied:  docker build --build-context scenario_engine=scenario-engine control-plane/worker
# (compose: additional_contexts; CI: build-contexts). Its runtime needs (httpx, pyyaml)
# are already in requirements.txt.
COPY --from=scenario_engine scenario_engine /app/scenario_engine
COPY . .
# Run as an unprivileged user (Trivy DS-0002). Dependencies and code stay root-owned,
# so a compromised process cannot rewrite the app it is running. The workspace dir is
# where TerraformProvisioner writes per-range state (provisioners/terraform.py:44).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /opt/truenorth/terraform/workspaces \
    && chown app:app /opt/truenorth/terraform/workspaces
USER app
CMD ["celery","-A","worker.celery_app","worker","--loglevel=INFO"]
