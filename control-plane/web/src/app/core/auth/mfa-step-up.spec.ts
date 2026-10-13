import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import {
  STEP_UP_KEY,
  STEP_UP_WINDOW_MS,
  clearMfaStepUp,
  isMfaRequired,
  requestMfaStepUp,
  resetStepUpForTests,
} from './mfa-step-up';

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => data.get(k) ?? null,
    key: (i: number) => [...data.keys()][i] ?? null,
    removeItem: (k: string) => void data.delete(k),
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

describe('MFA step-up', () => {
  let keycloak: { login: jasmine.Spy };
  let store: Storage;

  beforeEach(() => {
    resetStepUpForTests();
    keycloak = { login: jasmine.createSpy('login').and.resolveTo(undefined) };
    store = memoryStorage();
  });

  it('recognises the API refusal by its code', () => {
    const refusal = new HttpErrorResponse({
      status: 403,
      error: { detail: { code: 'mfa_required', state: 'mfa_required' } },
      headers: new HttpHeaders({ 'X-TrueNorth-Auth-State': 'mfa_required' }),
    });
    expect(isMfaRequired(refusal)).toBeTrue();
    expect(isMfaRequired(new HttpErrorResponse({ status: 403, error: { detail: 'Permission denied' } }))).toBeFalse();
    expect(isMfaRequired(new HttpErrorResponse({ status: 401, error: { detail: { code: 'mfa_required' } } }))).toBeFalse();
    expect(isMfaRequired(new Error('x'))).toBeFalse();
  });

  it('asks Keycloak for acr "mfa", essential', () => {
    expect(requestMfaStepUp(keycloak, 1_000_000, store)).toBe('redirecting');
    const options = keycloak.login.calls.mostRecent().args[0];
    expect(options.acr).toEqual({ values: ['mfa'], essential: true });
    expect(store.getItem(STEP_UP_KEY)).toBe('1000000');
  });

  it('starts one step-up per page load', () => {
    requestMfaStepUp(keycloak, 1_000_000, store);
    expect(requestMfaStepUp(keycloak, 1_000_001, store)).toBe('in_progress');
    expect(keycloak.login).toHaveBeenCalledTimes(1);
  });

  it('loop guard: back from a step-up and still refused -> exhausted, no new redirect', () => {
    store.setItem(STEP_UP_KEY, String(1_000_000)); // the attempt before the redirect
    expect(requestMfaStepUp(keycloak, 1_000_000 + 30_000, store)).toBe('exhausted');
    expect(keycloak.login).not.toHaveBeenCalled();
  });

  it('steps up again once the window has passed, or after a success cleared it', () => {
    store.setItem(STEP_UP_KEY, String(1_000_000));
    expect(requestMfaStepUp(keycloak, 1_000_000 + STEP_UP_WINDOW_MS + 1, store)).toBe('redirecting');
    resetStepUpForTests();
    clearMfaStepUp(store);
    expect(requestMfaStepUp(keycloak, 1_000_000 + STEP_UP_WINDOW_MS + 2, store)).toBe('redirecting');
  });
});
