import type { AppEnvironment } from './app-environment';

/**
 * Production. Swapped in for environment.ts by angular.json (`production`
 * configuration, fileReplacements), which is what the Dockerfile builds.
 *
 * The Keycloak values are defaults only: the web container rewrites
 * assets/config.json from TN_KEYCLOAK_URL / TN_KEYCLOAK_REALM /
 * TN_KEYCLOAK_CLIENT_ID at start-up, and the SPA applies it before bootstrap.
 * `/auth` is right wherever Keycloak is proxied under the app's own origin
 * (compose.prod.yml: KC_HTTP_RELATIVE_PATH=/auth).
 *
 * authDisabled must stay false here. scripts/check-prod-env.mjs enforces it.
 */
export const environment: AppEnvironment = {
  production: true,
  apiUrl: '/api',
  keycloak: {
    url: '/auth',
    realm: 'truenorth',
    clientId: 'truenorth-web',
  },
  authDisabled: false,
  // Only the Keycloak console (derived from keycloak.url) unless TN_ADMIN_* are set.
  adminLinks: {},
};
