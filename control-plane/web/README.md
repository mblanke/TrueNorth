# TrueNorth Range — Web UI

Angular 19 single-page application with Material M3 design system.

## Features
- Dashboard with range/exercise stats
- Range management (CRUD, provisioning, snapshots)
- Exercise lifecycle with real-time scoring
- Scenario builder with timeline editor
- Telemetry search and visualization
- Admin panel (users, roles, system health)
- Content catalog for templates and scenarios

## Development
```bash
npm install
ng serve --proxy-config proxy.conf.json
```

## Build
```bash
ng build --configuration production
npm run check:prod-env   # fails if the bundle would ship with authDisabled: true
```

## Auth
- Keycloak OIDC via `AuthService`
- Route guards: `authGuard`, `adminGuard`, `instructorGuard`
- HTTP interceptor adds Bearer token automatically

### Configuration per environment

| Where | Environment file | Auth | Keycloak endpoint |
|-------|------------------|------|-------------------|
| `npm start`, Karma specs | `environment.ts` | off (dev admin) | unused |
| Production build (Dockerfile, CI) | `environment.prod.ts` via angular.json `fileReplacements` | on | `/auth`, overridable at runtime |

The production image reads `assets/config.json` before bootstrap
(`src/app/core/config/runtime-config.ts`). The container writes it at start-up from
`TN_KEYCLOAK_URL`, `TN_KEYCLOAK_REALM` and `TN_KEYCLOAK_CLIENT_ID`
(`docker/40-truenorth-runtime-config.sh`); unset values keep the build defaults. Only
the Keycloak endpoint can be overridden: there is no runtime switch for `authDisabled`.

- `compose.dev.yml`: `TN_KEYCLOAK_URL=http://localhost:8180` (sign in with a dev-realm user).
- `compose.itest.yml`: `/auth`; the Playwright lane stubs the OIDC endpoints.
- `compose.prod.yml`: `/auth` (Keycloak under `https://${DOMAIN}/auth`), realm from `KEYCLOAK_REALM`.
- Helm: `web.keycloak.url` / `realm` / `clientId`; an absolute URL is added to the CSP.