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
  /**
   * Admin > Services consoles. `keycloak` defaults to `<keycloak.url>/admin/`; a card
   * whose link is unset is not shown. Runtime-overridable (TN_ADMIN_* in the container).
   */
  adminLinks: AdminLinks;
  /**
   * The Moodle site Learning > LMS previews links to when no Moodle platform is
   * registered under Integrations. Unset: no link. Runtime-overridable (TN_MOODLE_URL).
   */
  moodleUrl?: string;
  /**
   * The fictional LMS and readiness design previews (Learning > previews). Off unless a
   * deployment turns them on (TN_DEMO_PREVIEWS=true): their records are invented, so a
   * real Student must never mistake them for their own. On, they carry a demo banner.
   */
  demoPreviews?: boolean;
}

export interface AdminLinks {
  keycloak?: string;
  minio?: string;
  dashboards?: string;
  ai?: string;
}
