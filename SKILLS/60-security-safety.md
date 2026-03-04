# Security & Safety
## Secrets
- Never output secrets or tokens.
- Never commit credentials.
- Use .env files (gitignored) for local dev.
## Auth
- All API endpoints require JWT (except /health).
- RBAC enforced at middleware level.
- Keycloak for OIDC.
## Inputs
- Validate all external inputs at boundaries.
- Fail closed for auth/security decisions.
## Range isolation
- Per-range network isolation (VLANs/bridges).
- Per-tenant data isolation (tenant_id on all queries).
- No cross-tenant data leakage.
