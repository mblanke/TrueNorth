import { TestBed } from '@angular/core/testing';
import { HttpClient, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import Keycloak from 'keycloak-js';
import { environment } from '@env/environment';
import { authInterceptorProvider } from './auth.interceptor';
import { clearLtiSession, storeLtiSession } from '../auth/lti-session';
import { Router } from '@angular/router';
import { STEP_UP_KEY, resetStepUpForTests } from '../auth/mfa-step-up';

/** Resolve pending microtasks (the interceptor awaits the adapter's token refresh). */
const settle = () => new Promise<void>(resolve => setTimeout(resolve));

describe('AuthInterceptor', () => {
  const originalAuthDisabled = environment.authDisabled;
  let http: HttpClient;
  let backend: HttpTestingController;
  let keycloak: { authenticated: boolean; token?: string; updateToken: jasmine.Spy; login: jasmine.Spy };
  let router: { navigate: jasmine.Spy };

  function setup(authDisabled: boolean): void {
    (environment as { authDisabled: boolean }).authDisabled = authDisabled;
    keycloak = {
      authenticated: true,
      token: 'kc-access-token',
      updateToken: jasmine.createSpy('updateToken').and.resolveTo(false),
      login: jasmine.createSpy('login').and.resolveTo(undefined),
    };
    router = { navigate: jasmine.createSpy('navigate').and.resolveTo(true) };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        authInterceptorProvider,
        { provide: Keycloak, useValue: keycloak },
        { provide: Router, useValue: router },
      ],
    });
    http = TestBed.inject(HttpClient);
    backend = TestBed.inject(HttpTestingController);
  }

  afterEach(() => {
    backend.verify();
    (environment as { authDisabled: boolean }).authDisabled = originalAuthDisabled;
    document.cookie = 'truenorth_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
  });

  describe('with Keycloak enabled', () => {
    beforeEach(() => setup(false));

    it('attaches the adapter token as a bearer header', async () => {
      http.get('/api/ranges').subscribe();
      await settle();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.get('Authorization')).toBe('Bearer kc-access-token');
      expect(keycloak.updateToken).toHaveBeenCalledWith(10);
      req.flush([]);
    });

    it('sends no Authorization header when not signed in', async () => {
      keycloak.authenticated = false;
      http.get('/api/ranges').subscribe();
      await settle();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.has('Authorization')).toBeFalse();
      req.flush([]);
    });

    it('carries an LTI session for a Student launched from their LMS', async () => {
      keycloak.authenticated = false;
      storeLtiSession('lti-session-token', 3600);
      http.get('/api/quizzes/q1').subscribe();
      await settle();
      const req = backend.expectOne('/api/quizzes/q1');
      expect(req.request.headers.get('Authorization')).toBe('Bearer lti-session-token');
      req.flush({});
      clearLtiSession();
    });

    it('prefers a Keycloak sign-in over an LTI session', async () => {
      storeLtiSession('lti-session-token', 3600);
      http.get('/api/ranges').subscribe();
      await settle();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.get('Authorization')).toBe('Bearer kc-access-token');
      req.flush([]);
      clearLtiSession();
    });

    describe('MFA step-up (403 mfa_required)', () => {
      const refusal = { detail: { code: 'mfa_required', state: 'mfa_required', message: 'x', next: '/auth/me' } };

      beforeEach(() => {
        resetStepUpForTests();
        sessionStorage.removeItem(STEP_UP_KEY);
      });
      afterEach(() => sessionStorage.removeItem(STEP_UP_KEY));

      it('signs in again asking for acr "mfa"', async () => {
        http.get('/api/ranges').subscribe({ error: () => undefined });
        await settle();
        backend.expectOne('/api/ranges').flush(refusal, { status: 403, statusText: 'Forbidden' });
        expect(keycloak.login).toHaveBeenCalledTimes(1);
        expect(keycloak.login.calls.mostRecent().args[0].acr).toEqual({ values: ['mfa'], essential: true });
        expect(router.navigate).not.toHaveBeenCalled();
      });

      it('does not loop: refused again after a step-up, it shows /mfa-required', async () => {
        sessionStorage.setItem(STEP_UP_KEY, String(Date.now() - 10_000));
        http.get('/api/ranges').subscribe({ error: () => undefined });
        await settle();
        backend.expectOne('/api/ranges').flush(refusal, { status: 403, statusText: 'Forbidden' });
        expect(keycloak.login).not.toHaveBeenCalled();
        expect(router.navigate).toHaveBeenCalledWith(['/mfa-required']);
      });

      it('leaves an ordinary 403 alone', async () => {
        http.get('/api/ranges').subscribe({ error: () => undefined });
        await settle();
        backend.expectOne('/api/ranges').flush({ detail: 'Permission denied' }, { status: 403, statusText: 'Forbidden' });
        expect(keycloak.login).not.toHaveBeenCalled();
        expect(router.navigate).not.toHaveBeenCalled();
      });
    });

    it('leaves static assets alone', () => {
      http.get('/assets/silent-check-sso.html', { responseType: 'text' }).subscribe();
      const req = backend.expectOne('/assets/silent-check-sso.html');
      expect(req.request.headers.has('Authorization')).toBeFalse();
      req.flush('');
    });
  });

  describe('CSRF double-submit', () => {
    beforeEach(() => {
      setup(true);
      document.cookie = 'truenorth_csrf=csrf-123; path=/';
    });

    it('echoes the cookie in X-CSRF-Token on unsafe methods', () => {
      http.post('/api/ranges', {}).subscribe();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.get('X-CSRF-Token')).toBe('csrf-123');
      req.flush({});
    });

    it('does not add it to safe methods', () => {
      http.get('/api/ranges').subscribe();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.has('X-CSRF-Token')).toBeFalse();
      req.flush([]);
    });

    it('never asks Keycloak for a token while auth is disabled', () => {
      http.get('/api/ranges').subscribe();
      const req = backend.expectOne('/api/ranges');
      expect(req.request.headers.has('Authorization')).toBeFalse();
      expect(keycloak.updateToken).not.toHaveBeenCalled();
      req.flush([]);
    });
  });
});
