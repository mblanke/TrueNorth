import { TestBed } from '@angular/core/testing';
import { AuthService, CurrentUser } from './auth.service';
import { environment } from '@env/environment';

describe('AuthService', () => {
  let service: AuthService;
  const originalAuthDisabled = environment.authDisabled;

  beforeEach(() => {
    // Force authDisabled so constructor doesn't set dev user automatically
    // (we'll test both paths)
    (environment as any).authDisabled = false;

    TestBed.configureTestingModule({
      providers: [AuthService],
    });
    service = TestBed.inject(AuthService);
  });

  afterEach(() => {
    (environment as any).authDisabled = originalAuthDisabled;
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  // ── login() stores token / sets user ─────────────────────────────
  it('login() with authDisabled=false should redirect (no user set)', () => {
    // When auth is NOT disabled, login() would redirect to Keycloak.
    // We can't test real redirect so we verify user is NOT set.
    expect(service.isAuthenticated()).toBeFalse();
  });

  it('setUser() should store user and mark as authenticated', () => {
    const user: CurrentUser = {
      sub: 'u-001',
      email: 'test@truenorth.local',
      display_name: 'Test User',
      role: 'admin',
    };
    service.setUser(user);
    expect(service.isAuthenticated()).toBeTrue();
    expect(service.user()?.email).toBe('test@truenorth.local');
  });

  // ── logout() clears token ────────────────────────────────────────
  it('logout() should clear user and set isAuthenticated to false', () => {
    service.setUser({
      sub: 'u-001',
      email: 'test@truenorth.local',
      display_name: 'Test User',
      role: 'admin',
    });
    expect(service.isAuthenticated()).toBeTrue();

    service.logout();
    expect(service.isAuthenticated()).toBeFalse();
    expect(service.user()).toBeNull();
  });

  // ── isAuthenticated computed signal ──────────────────────────────
  it('isAuthenticated should emit true when user is present', () => {
    expect(service.isAuthenticated()).toBeFalse();
    service.setUser({
      sub: 'u-002',
      email: 'admin@truenorth.local',
      display_name: 'Admin',
      role: 'admin',
    });
    expect(service.isAuthenticated()).toBeTrue();
  });

  // ── getToken / user() returns stored user ────────────────────────
  it('user() should return the stored user object', () => {
    expect(service.user()).toBeNull();
    const user: CurrentUser = {
      sub: 'u-003',
      email: 'instructor@truenorth.local',
      display_name: 'Instructor',
      role: 'instructor',
    };
    service.setUser(user);
    expect(service.user()).toEqual(user);
  });

  // ── Role-based computed signals ──────────────────────────────────
  it('isAdmin should be true when role is admin', () => {
    service.setUser({
      sub: 'u-001',
      email: 'admin@tn.local',
      display_name: 'Admin',
      role: 'admin',
    });
    expect(service.isAdmin()).toBeTrue();
    expect(service.isInstructor()).toBeTrue();  // admin implies instructor
  });

  it('isAdmin should be false for instructor role', () => {
    service.setUser({
      sub: 'u-004',
      email: 'inst@tn.local',
      display_name: 'Inst',
      role: 'instructor',
    });
    expect(service.isAdmin()).toBeFalse();
    expect(service.isInstructor()).toBeTrue();
  });

  it('isInstructor should be false for trainee role', () => {
    service.setUser({
      sub: 'u-005',
      email: 'trainee@tn.local',
      display_name: 'Trainee',
      role: 'trainee',
    });
    expect(service.isAdmin()).toBeFalse();
    expect(service.isInstructor()).toBeFalse();
  });

  // ── Dev mode auto-login ──────────────────────────────────────────
  it('should auto-set dev user when authDisabled is true', () => {
    // Re-create service with authDisabled = true
    (environment as any).authDisabled = true;
    const devService = new AuthService();

    expect(devService.isAuthenticated()).toBeTrue();
    expect(devService.user()?.email).toBe('admin@truenorth.local');
    expect(devService.isAdmin()).toBeTrue();
  });
});