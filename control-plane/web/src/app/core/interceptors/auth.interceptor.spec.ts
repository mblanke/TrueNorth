import { TestBed } from '@angular/core/testing';
import { HttpClient, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import Keycloak from 'keycloak-js';
import { environment } from '@env/environment';
import { authInterceptorProvider } from './auth.interceptor';
import { clearLtiSession, storeLtiSession } from '../auth/lti-session';

/** Resolve pending microtasks (the interceptor awaits the adapter's token refresh). */
const settle = () => new Promise<void>(resolve => setTimeout(resolve));

describe('AuthInterceptor', () => {
  const originalAuthDisabled = environment.authDisabled;
  let http: HttpClient;
  let backend: HttpTestingController;
  let keycloak: { authenticated: boolean; token?: string; updateToken: jasmine.Spy };

  function setup(authDisabled: boolean): void {
    (environment as { authDisabled: boolean }).authDisabled = authDisabled;
    keycloak = {
      authenticated: true,
      token: 'kc-access-token',
      updateToken: jasmine.createSpy('updateToken').and.resolveTo(false),
    };
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        authInterceptorProvider,
        { provide: Keycloak, useValue: keycloak },
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
