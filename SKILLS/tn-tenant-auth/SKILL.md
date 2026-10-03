---
name: tn-tenant-auth
description: "Review or repair TrueNorth authentication, RBAC, tenant isolation, and network exposure when security boundaries are in scope."
---

# Tenant and identity boundaries

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/app/auth.py`
- `control-plane/api/app/tenancy.py`
- `control-plane/api/app/rbac.py`
- `tests/api/test_tenant_isolation.py`
- `infra/platform/docker/compose.dev.yml`
- `control-plane/web/nginx.conf`

## Workflow

1. Trace the caller from public listener through reverse proxy, identity validation, role resolution, tenant query, and returned or mutated resource.
2. Inspect normal endpoints, nested resources, batch IDs, search indices, object keys, WebSockets, and background jobs relevant to the change.
3. Use get_owned for tenant mutations; use get_owned_or_global only where shared read access is intended. Verify the actual business rule before changing permissions.
4. Check the full exposure path: a loopback API can remain externally reachable through a published web proxy. Confirm configuration separately from observed network reachability.
5. Treat regex guard tests as supplemental. Add behavior cases using two tenants and distinct privilege levels for affected operations.
6. Keep secrets out of findings and commands. Give a minimal reproduction with synthetic identities and scoped local resources.

## Verification

Show that authorized access still works and that foreign-tenant or unprivileged access fails without resource disclosure or mutation.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Ranked findings or requested fixes, each with a concrete boundary, file location, reproduction, and regression evidence.

## Boundary

Review is read-only unless fixes are requested. Shell tools do not authorize probing unrelated hosts.

