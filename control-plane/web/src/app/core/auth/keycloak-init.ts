import { KeycloakService } from 'keycloak-angular';
import { environment } from '@env/environment';

/**
 * Keycloak bootstrap, run once via APP_INITIALIZER.
 *
 * We use keycloak-angular rather than hand-rolling the OIDC flow. The previous
 * hand-rolled redirect in AuthService sent no `code_challenge`, while the realm
 * sets `pkce.code.challenge.method: S256` on the `truenorth-web` client — so
 * Keycloak rejected the request outright. Finishing it by hand would mean owning
 * WebCrypto PKCE, state/nonce, the token exchange, a refresh timer and
 * id_token_hint logout: security-critical code with no upside, when the adapter
 * is already a dependency and version-matched to the Keycloak 24 server.
 *
 * `check-sso` rather than `login-required`: the app must be able to render the
 * login page and the public registration route for an anonymous visitor. Route
 * guards decide what needs authentication.
 */
export function initializeKeycloak(keycloak: KeycloakService): () => Promise<boolean> {
  return async () => {
    if (environment.authDisabled) {
      // Dev mode mirrors the API's AUTH_DISABLED short-circuit. Skipping init
      // entirely keeps `npm start` working with no Keycloak running at all.
      return true;
    }

    const authenticated = await keycloak.init({
      config: {
        url: environment.keycloak.url,
        realm: environment.keycloak.realm,
        clientId: environment.keycloak.clientId,
      },
      initOptions: {
        onLoad: 'check-sso',
        silentCheckSsoRedirectUri: `${window.location.origin}/assets/silent-check-sso.html`,
        pkceMethod: 'S256',
        // The login-status iframe is blocked by third-party-cookie policies in
        // current browsers and produces spurious logouts. Silent check-sso plus
        // token refresh covers the same ground.
        checkLoginIframe: false,
      },
      // The adapter must not attach a bearer token to static assets, and the
      // API is reached through the same origin via nginx.
      bearerExcludedUrls: ['/assets', '/silent-check-sso.html'],
    });

    // Refresh 30s before expiry rather than letting a request fail first.
    const instance = keycloak.getKeycloakInstance();
    instance.onTokenExpired = () => {
      keycloak.updateToken(30).catch(() => keycloak.login());
    };

    return authenticated;
  };
}
