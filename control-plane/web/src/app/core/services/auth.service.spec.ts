import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { KeycloakService } from 'keycloak-angular';
import { AuthService, CurrentUser } from './auth.service';
import { environment } from '@env/environment';

/**
 * These run with `authDisabled = true`, which short-circuits the Keycloak
 * adapter exactly as the API's AUTH_DISABLED short-circuits token validation.
 * A KeycloakService stub is still provided because the service injects it.
 */
describe('AuthService', () => {
  let service: AuthService;
  const originalAuthDisabled = environment.authDisabled;

  const keycloakStub = {
    isLoggedIn: () => Promise.resolve(false),
    getToken: () => Promise.resolve(''),
    getKeycloakInstance: () => ({ subject: 'kc-sub' }),
    login: () => Promise.resolve(),
    logout: () => Promise.resolve(),
  };

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

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [AuthService, { provide: KeycloakService, useValue: keycloakStub }],
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
});
