import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import Keycloak from 'keycloak-js';
import { freshToken } from '../auth/keycloak-init';
import { AuthService, CurrentUser } from './auth.service';
import { environment } from '@env/environment';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';

/**
 * These run with `authDisabled = true`, which short-circuits the Keycloak
 * adapter exactly as the API's AUTH_DISABLED short-circuits token validation.
 * A keycloak-js stub is still provided because the service injects it.
 */
describe('AuthService', () => {
  let service: AuthService;
  const originalAuthDisabled = environment.authDisabled;

  let keycloakStub: { authenticated: boolean; subject?: string; token?: string; updateToken: jasmine.Spy; login: jasmine.Spy; logout: jasmine.Spy };

  const user = (over: Partial<CurrentUser> = {}): CurrentUser => ({
    sub: 'u-001',
    id: '11111111-1111-1111-1111-111111111111',
    email: 'test@truenorth.local',
    display_name: 'Test User',
    role: 'admin',
    ...over,
  });

  beforeEach(() => {
    // Force authDisabled so login/logout don't trigger window.location redirects
    (environment as any).authDisabled = true;
    keycloakStub = {
      authenticated: false,
      subject: 'kc-sub',
      updateToken: jasmine.createSpy('updateToken').and.resolveTo(true),
      login: jasmine.createSpy('login').and.resolveTo(),
      logout: jasmine.createSpy('logout').and.resolveTo(),
    };

    TestBed.configureTestingModule({
    imports: [],
    providers: [AuthService, { provide: Keycloak, useValue: keycloakStub }, provideHttpClient(withInterceptorsFromDi()), provideHttpClientTesting()]
});
    service = TestBed.inject(AuthService);
  });

  afterEach(() => {
    (environment as any).authDisabled = originalAuthDisabled;
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('login() with authDisabled=true should keep dev user', () => {
    expect(service.isAuthenticated()).toBeTrue();
  });

  it('setUser() should store user and mark as authenticated', () => {
    service.setUser(user());
    expect(service.isAuthenticated()).toBeTrue();
    expect(service.user()?.email).toBe('test@truenorth.local');
  });

  it('userId() exposes the database id, not the token subject', () => {
    service.setUser(user());
    expect(service.userId()).toBe('11111111-1111-1111-1111-111111111111');
  });

  it('logout() should clear user and set isAuthenticated to false', () => {
    service.setUser(user());
    service.logout();
    expect(service.isAuthenticated()).toBeFalse();
    expect(service.user()).toBeNull();
  });

  // ── Role predicates ──────────────────────────────────────────────
  it('isAdmin should be true for admin role', () => {
    service.setUser(user({ role: 'admin' }));
    expect(service.isAdmin()).toBeTrue();
    expect(service.isInstructor()).toBeTrue();
  });

  it('isAdmin should be false for instructor role', () => {
    service.setUser(user({ sub: 'u-004', email: 'inst@tn.local', role: 'instructor' }));
    expect(service.isAdmin()).toBeFalse();
    expect(service.isInstructor()).toBeTrue();
  });

  it('isInstructor should be false for student role', () => {
    // The backend enum is `student`; the frontend previously used a `trainee`
    // value that exists nowhere server-side.
    service.setUser(user({ sub: 'u-005', email: 'trainee@tn.local', role: 'student' }));
    expect(service.isAdmin()).toBeFalse();
    expect(service.isInstructor()).toBeFalse();
  });

  // ── Onboarding gate ──────────────────────────────────────────────
  it('needsOnboarding is true for an unfinished student', () => {
    service.setUser(user({ role: 'student', onboarding_state: 'profile' }));
    expect(service.needsOnboarding()).toBeTrue();
  });

  it('needsOnboarding is false once a student completes', () => {
    service.setUser(user({ role: 'student', onboarding_state: 'complete' }));
    expect(service.needsOnboarding()).toBeFalse();
  });

  it('needsOnboarding never traps an admin', () => {
    service.setUser(user({ role: 'admin', onboarding_state: 'not_started' }));
    expect(service.needsOnboarding()).toBeFalse();
  });

  // ── Dev mode auto-login ──────────────────────────────────────────
  it('should auto-set dev user when authDisabled is true', () => {
    expect(service.isAuthenticated()).toBeTrue();
    expect(service.user()?.email).toBe('admin@truenorth.local');
    expect(service.isAdmin()).toBeTrue();
  });

  // ── With Keycloak enabled (keycloak-js instance, provideKeycloak) ──
  describe('with auth enabled', () => {
    beforeEach(() => ((environment as any).authDisabled = false));

    it('bootstrap() is signed-out when Keycloak is not authenticated, without calling the API', async () => {
      const http = TestBed.inject(HttpTestingController);
      expect(await service.bootstrap(true)).toBeNull();
      expect(service.user()).toBeNull();
      http.expectNone(`${environment.apiUrl}/auth/me`);
    });

    it('bootstrap() takes the user from /auth/me and the token subject from Keycloak', async () => {
      keycloakStub.authenticated = true;
      const http = TestBed.inject(HttpTestingController);
      const pending = service.bootstrap(true);
      http.expectOne(`${environment.apiUrl}/auth/me`).flush({
        status: 'registered',
        user: { id: 'db-1', email: 'a@tn.local', display_name: 'A', role: 'student' },
      });
      await pending;
      expect(service.user()?.sub).toBe('kc-sub');
      expect(service.userId()).toBe('db-1');
    });

    it('login() and logout() hand off to Keycloak with this origin', () => {
      service.login();
      expect(keycloakStub.login).toHaveBeenCalledWith({ redirectUri: window.location.origin });
      service.logout();
      expect(keycloakStub.logout).toHaveBeenCalledWith({ redirectUri: window.location.origin });
    });
  });

  describe('freshToken', () => {
    it('is empty when not signed in, and does not try to refresh', async () => {
      expect(await freshToken(keycloakStub as unknown as Keycloak)).toBe('');
      expect(keycloakStub.updateToken).not.toHaveBeenCalled();
    });

    it('refreshes a token about to expire, then returns the current one', async () => {
      keycloakStub.authenticated = true;
      keycloakStub.updateToken.and.callFake(async () => {
        keycloakStub.token = 'new';
        return true;
      });
      expect(await freshToken(keycloakStub as unknown as Keycloak)).toBe('new');
      expect(keycloakStub.updateToken).toHaveBeenCalledWith(10);
    });

    it('returns the held token when refresh fails, so the API can answer 401', async () => {
      keycloakStub.authenticated = true;
      keycloakStub.token = 'old';
      keycloakStub.updateToken.and.rejectWith(new Error('refresh failed'));
      expect(await freshToken(keycloakStub as unknown as Keycloak)).toBe('old');
    });
  });
});
