/**
 * The LTI session hand-off (control-plane/api/app/lti_identity/session.py).
 *
 * A Student who arrives from Moodle with no TrueNorth sign-in of their own gets a short
 * TrueNorth session instead of a Keycloak one: `/lti/session#code=…` exchanges the code
 * once for a bearer token, kept here for this tab only (sessionStorage, never
 * localStorage) until it expires. It is not renewable: a new launch gives a new one.
 * A Keycloak sign-in, when there is one, always wins (see AuthInterceptor).
 */

const KEY = 'tn.lti-session';

interface Stored {
  token: string;
  expiresAt: number; // epoch ms
}

function read(): Stored | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Stored;
    if (!value?.token || typeof value.expiresAt !== 'number' || value.expiresAt <= Date.now()) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

/** The current LTI session token, or '' when there is none (or it has expired). */
export function ltiSessionToken(): string {
  return read()?.token ?? '';
}

export function hasLtiSession(): boolean {
  return read() !== null;
}

/** Keep a token for `expiresInSeconds`, less a margin so a request never carries a stale one. */
export function storeLtiSession(token: string, expiresInSeconds: number): void {
  const margin = Math.min(60, Math.max(0, expiresInSeconds - 1));
  sessionStorage.setItem(KEY, JSON.stringify({ token, expiresAt: Date.now() + (expiresInSeconds - margin) * 1000 }));
}

export function clearLtiSession(): void {
  sessionStorage.removeItem(KEY);
}

/** The hand-off code from a URL fragment (`#code=…`), or null. */
export function handoffCode(fragment: string | null | undefined): string | null {
  if (!fragment) return null;
  const params = new URLSearchParams(fragment.startsWith('#') ? fragment.slice(1) : fragment);
  const code = params.get('code');
  return code && /^[A-Za-z0-9_-]{16,128}$/.test(code) ? code : null;
}
