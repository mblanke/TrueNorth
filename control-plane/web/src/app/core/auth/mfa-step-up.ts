import { HttpErrorResponse } from '@angular/common/http';
import type Keycloak from 'keycloak-js';

/**
 * MFA step-up (v1.2.0).
 *
 * The API refuses a privileged role (admin, instructor, range_ops, observer) whose token
 * shows no second factor: 403 with `detail.code = "mfa_required"` on any route, and
 * `/auth/me` reports `mfa_required && !mfa_satisfied`. The SPA then signs in again asking
 * Keycloak for acr "mfa"; the realm's tn-browser flow answers with the OTP prompt (or
 * authenticator enrolment) and the new token passes.
 *
 * Loop guard: one step-up per window. If the token after the step-up still does not
 * satisfy the API (no authenticator could be enrolled, or the realm is misconfigured),
 * the app shows /mfa-required ("contact your administrator") instead of redirecting
 * forever.
 */
export const STEP_UP_KEY = 'tn_mfa_step_up_at';
export const STEP_UP_WINDOW_MS = 5 * 60 * 1000;
/** What keycloak-js sends as `claims={"id_token":{"acr":{...}}}`. */
export const STEP_UP_ACR = { values: ['mfa'], essential: true };

/**
 * 'redirecting': a step-up sign-in was started; 'in_progress': one already was in this
 * page load (the browser is leaving; do nothing); 'exhausted': one was tried within the
 * window and the API still refuses, so show /mfa-required.
 */
export type StepUpOutcome = 'redirecting' | 'in_progress' | 'exhausted';

let startedThisPage = false;

/** Test seam: forget that a step-up was started in this page load. */
export function resetStepUpForTests(): void {
  startedThisPage = false;
}

/** True for the API's MFA refusal (app/mfa_policy.py MFA_REQUIRED_DETAIL). */
export function isMfaRequired(err: unknown): boolean {
  if (!(err instanceof HttpErrorResponse) || err.status !== 403) {
    return false;
  }
  const detail = (err.error as { detail?: unknown } | null)?.detail;
  return typeof detail === 'object' && detail !== null && (detail as { code?: unknown }).code === 'mfa_required';
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function lastAttempt(store: Storage | null): number {
  try {
    return Number(store?.getItem(STEP_UP_KEY) ?? 0) || 0;
  } catch {
    return 0;
  }
}

/**
 * Start a step-up sign-in, unless one was already tried within the window: then
 * 'exhausted', and the caller shows /mfa-required.
 */
export function requestMfaStepUp(
  keycloak: Pick<Keycloak, 'login'>,
  now: number = Date.now(),
  store: Storage | null = storage(),
): StepUpOutcome {
  if (startedThisPage) {
    return 'in_progress';
  }
  if (now - lastAttempt(store) < STEP_UP_WINDOW_MS) {
    return 'exhausted';
  }
  startedThisPage = true;
  try {
    store?.setItem(STEP_UP_KEY, String(now));
  } catch {
    // no storage: still step up once; a second refusal in this page load will loop at most once more
  }
  void keycloak.login({ acr: STEP_UP_ACR, redirectUri: window.location.href });
  return 'redirecting';
}

/** Forget the attempt once the API is satisfied, so a later expiry can step up again. */
export function clearMfaStepUp(store: Storage | null = storage()): void {
  try {
    store?.removeItem(STEP_UP_KEY);
  } catch {
    // nothing to clear
  }
}
