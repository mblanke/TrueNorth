/**
 * A stand-in for Keycloak's browser endpoints, so the journeys can reach guarded pages.
 *
 * What is real and what is not, in the e2e lane (scripts/itest.sh with ITEST_WEB=1):
 * - The SPA is the production build, served by the web container's nginx on :14200.
 * - The API is real and runs with AUTH_DISABLED=true, so it answers as the seeded dev
 *   admin (keycloak_id "dev-admin") whatever bearer token arrives. `/auth/me` therefore
 *   returns a registered admin once the SPA believes it is signed in.
 * - Keycloak is NOT exercised. The SPA's keycloak-js adapter only needs `authenticated`
 *   to be true, and that is decided in the browser by the check-sso iframe and the token
 *   exchange. Those three requests are answered here. A real sign-in (realm redirect URIs,
 *   the /auth path prefix, the login form) is outside what this lane proves.
 *
 * The tokens below are unsigned and only ever seen by the browser and an API that ignores
 * them. Nothing here changes server-side identity, roles or permissions.
 */
import type { BrowserContext, Route } from '@playwright/test';

const OIDC = /\/realms\/[^/]+\/protocol\/openid-connect\//;

function b64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

/** An unsigned JWT with the claims keycloak-js reads (exp, iat) plus the dev identity. */
function fakeJwt(typ: 'Bearer' | 'Refresh'): string {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iat: now,
    exp: now + 3600,
    typ,
    sub: 'dev-admin',
    azp: 'truenorth-web',
    preferred_username: 'dev-admin',
    email: 'admin@truenorth.local',
    name: 'Dev Admin',
    realm_access: { roles: ['admin'] },
  };
  return `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url(payload)}.e2e`;
}

function html(body: string): Parameters<Route['fulfill']>[0] {
  return { status: 200, contentType: 'text/html', body: `<!doctype html><html><body>${body}</body></html>` };
}

export interface IdentityStub {
  /** OIDC requests answered so far. Zero after a page load means the SPA was built with
   *  `authDisabled: true` and never asked an identity provider anything. */
  requests: number;
}

/**
 * Answer the adapter's OIDC traffic for every page in `context`.
 *
 * signedIn=true: check-sso returns an authorization code and the token endpoint swaps it
 * for the tokens above. signedIn=false: check-sso returns `login_required`, which is what
 * Keycloak says to an anonymous visitor, and the SPA routes guarded pages to /login.
 */
export async function stubIdentityProvider(
  context: BrowserContext,
  { signedIn }: { signedIn: boolean },
): Promise<IdentityStub> {
  const stub: IdentityStub = { requests: 0 };
  await context.route(OIDC, async (route) => {
    stub.requests += 1;
    const url = new URL(route.request().url());
    const endpoint = url.pathname.split('/protocol/openid-connect/')[1] ?? '';

    if (endpoint.startsWith('3p-cookies/')) {
      // keycloak-js probes third-party-cookie support before check-sso.
      return route.fulfill(html('<script>parent.postMessage("supported", "*");</script>'));
    }

    if (endpoint === 'auth') {
      // The silent check-sso iframe. Send it back to silent-check-sso.html, as Keycloak
      // would, with the callback in the fragment (response_mode=fragment).
      const redirect = url.searchParams.get('redirect_uri') ?? '';
      const state = url.searchParams.get('state') ?? '';
      const callback = signedIn
        ? `state=${encodeURIComponent(state)}&session_state=e2e&code=e2e-code`
        : `error=login_required&state=${encodeURIComponent(state)}`;
      const target = JSON.stringify(`${redirect}#${callback}`);
      return route.fulfill(html(`<script>location.replace(${target});</script>`));
    }

    if (endpoint === 'token' && signedIn) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: fakeJwt('Bearer'),
          refresh_token: fakeJwt('Refresh'),
          token_type: 'Bearer',
          expires_in: 3600,
          refresh_expires_in: 3600,
          session_state: 'e2e',
        }),
      });
    }

    return route.fulfill({ status: 400, contentType: 'application/json', body: '{"error":"invalid_request"}' });
  });
  return stub;
}
