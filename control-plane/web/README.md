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
```

## Auth
- Keycloak OIDC via `AuthService`
- Route guards: `authGuard`, `adminGuard`, `instructorGuard`
- HTTP interceptor adds Bearer token automatically
- Set `authDisabled: true` in environment.ts for development