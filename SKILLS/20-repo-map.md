# Repo Mapping Skill
When entering this repo:
- Purpose: TrueNorth Range — self-hosted cyber range platform (SimSpace/Cyberbit replacement)
- Key modules: control-plane/api (FastAPI), control-plane/worker (Celery), control-plane/web (Angular), scenario-engine/, ai-orchestrator/, telemetry/
- Data flow: Angular UI -> FastAPI API -> Postgres + Redis -> Celery Worker -> Terraform/Mock Provisioner -> Proxmox
- Commands: docker compose -f infra/platform/docker/compose.dev.yml up -d --build
- Config: .env.example (copy to .env)
