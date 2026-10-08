/**
 * Shape shared by environment.ts (development, `ng serve`, unit tests) and
 * environment.prod.ts (every production build: angular.json's `production`
 * configuration swaps it in with fileReplacements).
 *
 * `authDisabled` is build-time only. The runtime config (assets/config.json, see
 * core/config/runtime-config.ts) can move the Keycloak endpoint but can never turn
 * authentication off. scripts/check-prod-env.mjs fails the build if a production
 * bundle carries `authDisabled: true`.
 */
export interface AppEnvironment {
  production: boolean;
  apiUrl: string;
  keycloak: {
    url: string;
    realm: string;
    clientId: string;
  };
  authDisabled: boolean;
}
