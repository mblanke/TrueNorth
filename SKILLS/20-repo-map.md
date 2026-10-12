# Repo Mapping Skill
When entering this repo:
- Purpose: TrueNorth Range — self-hosted cyber range platform (SimSpace/Cyberbit replacement)
- Key modules: control-plane/api (FastAPI), control-plane/worker (Celery), control-plane/web (Angular), scenario-engine/, ai-orchestrator/, telemetry/, greyspace/, noise-agent/, install/ (Ansible installer), content/
- Data flow: Angular UI -> nginx (`/api/` on :4200) -> FastAPI API -> Postgres + Redis -> Celery worker -> provisioner registry -> vCenter (pyVmomi)
- Provisioner registry: `control-plane/worker/worker/provisioners/__init__.py`. Supported: `vsphere_api` (ADR 0009; helpers `vsphere_guest`, `vsphere_infra`, `vsphere_roles`) and `mock`. `proxmox_api` and `hyperv` are registered but refused unless `EXPERIMENTAL_PROVISIONERS` is true (the API refuses them too: `control-plane/api/app/provisioner_choice.py`). The Terraform backends were removed in #131; `infra/proxmox/terraform/` is not used by the worker
- vSphere evidence is simulator-only (vcsim, CI `vsphere-sim`) until a live lab run exists
- Other external systems sit behind `control-plane/api/app/*_backends/` adapters (ADR 0001)
- Commands: docker compose -f infra/platform/docker/compose.dev.yml up -d --build
- Config: .env.example (copy to .env)
