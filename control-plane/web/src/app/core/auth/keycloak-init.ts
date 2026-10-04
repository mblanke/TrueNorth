import { EnvironmentProviders, inject, provideAppInitializer } from '@angular/core';
import Keycloak from 'keycloak-js';
import { provideKeycloak } from 'keycloak-angular';
import { environment } from '@env/environment';

/**
 * Keycloak bootstrap via keycloak-angular's provideKeycloak().
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
 *
 * The bearer token is attached by AuthInterceptor (with the CSRF header), not by
 * keycloak-angular's interceptor.
 */
export function provideTrueNorthKeycloak(): EnvironmentProviders {
  return provideKeycloak({
    config: {
      url: environment.keycloak.url,
      realm: environment.keycloak.realm,
      clientId: environment.keycloak.clientId,
    },
    // Dev mode mirrors the API's AUTH_DISABLED short-circuit: without initOptions
    // the instance exists (services inject it) but never contacts Keycloak, so
    // `npm start` works with no Keycloak running at all.
    initOptions: environment.authDisabled
      ? undefined
      : {
          onLoad: 'check-sso',
          silentCheckSsoRedirectUri: `${window.location.origin}/assets/silent-check-sso.html`,
          pkceMethod: 'S256',
          // The login-status iframe is blocked by third-party-cookie policies in
          // current browsers and produces spurious logouts. Silent check-sso plus
          // token refresh covers the same ground.
          checkLoginIframe: false,
        },
    providers: [provideAppInitializer(installTokenRefresh)],
  });
}

/** Refresh when the access token expires rather than letting a request fail first. */
function installTokenRefresh(): void {
  if (environment.authDisabled) {
    return;
  }
  const keycloak = inject(Keycloak);
  keycloak.onTokenExpired = () => {
    keycloak.updateToken(30).catch(() => keycloak.login());
  };
}

/**
 * A current access token, refreshed if it expires within 10 s; '' when not signed
 * in. This is what KeycloakService.getToken() did. A failed refresh still returns
 * the old token, so the API answers 401 and the app can react.
 */
export async function freshToken(keycloak: Keycloak): Promise<string> {
  if (!keycloak.authenticated) {
    return '';
  }
  try {
    await keycloak.updateToken(10);
  } catch {
    // fall through with whatever token we hold
  }
  return keycloak.token ?? '';
}
