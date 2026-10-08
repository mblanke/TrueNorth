import type { AppEnvironment } from './app-environment';

/**
 * Development: `ng serve` (`npm start`) and the Karma unit specs. Every visitor is
 * the dev admin and no token is sent, mirroring the API's AUTH_DISABLED=true.
 *
 * Never shipped: production builds replace this file with environment.prod.ts
 * (angular.json fileReplacements), and scripts/check-prod-env.mjs fails the build
 * if that replacement goes missing.
 */
export const environment: AppEnvironment = {
  production: false,
  apiUrl: '/api',
  keycloak: {
    url: 'http://localhost:8180',
    realm: 'truenorth',
    clientId: 'truenorth-web',
  },
  authDisabled: true,
};
