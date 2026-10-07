import { TestBed } from '@angular/core/testing';
import { computed, signal } from '@angular/core';
import { ActivatedRouteSnapshot, Router, RouterStateSnapshot, UrlTree, provideRouter } from '@angular/router';
import { Observable, firstValueFrom } from 'rxjs';
import { AuthService, AuthState, UserRole } from '../services/auth.service';
import { adminGuard, authGuard } from './auth.guard';

/**
 * The guards read AuthService only after `bootstrap()` resolves (identity comes from
 * GET /auth/me). The fake below records that ordering: its state is set by `bootstrap`,
 * so a guard that read the signals synchronously would see the pre-bootstrap values.
 */
class FakeAuthService {
  private readonly role = signal<UserRole | null>(null);
  private readonly state = signal<AuthState | null>(null);
  readonly authState = this.state.asReadonly();
  readonly isAuthenticated = computed(() => this.role() !== null);
  readonly isAdmin = computed(() => this.role() === 'admin');

  next: { role: UserRole | null; state: AuthState | null } = { role: null, state: null };
  readonly bootstrap = jasmine.createSpy('bootstrap').and.callFake(async () => {
    this.role.set(this.next.role);
    this.state.set(this.next.state);
    return null;
  });
}

describe('auth guards', () => {
  let auth: FakeAuthService;
  let router: Router;

  beforeEach(() => {
    auth = new FakeAuthService();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: AuthService, useValue: auth }],
    });
    router = TestBed.inject(Router);
  });

  function run(guard: typeof authGuard): Promise<boolean | UrlTree> {
    const result = TestBed.runInInjectionContext(() =>
      guard({} as ActivatedRouteSnapshot, {} as RouterStateSnapshot),
    ) as Observable<boolean | UrlTree>;
    return firstValueFrom(result);
  }

  function path(result: boolean | UrlTree): string | boolean {
    return result instanceof UrlTree ? router.serializeUrl(result) : result;
  }

  describe('authGuard', () => {
    it('admits a registered account, after identity has resolved', async () => {
      auth.next = { role: 'student', state: 'registered' };
      expect(await run(authGuard)).toBeTrue();
      expect(auth.bootstrap).toHaveBeenCalled();
    });

    it('sends an anonymous visitor to /login', async () => {
      expect(path(await run(authGuard))).toBe('/login');
    });

    it('sends an identity with no account to /register', async () => {
      auth.next = { role: null, state: 'unregistered' };
      expect(path(await run(authGuard))).toBe('/register');
    });

    it('sends a pending or rejected request to the waiting room', async () => {
      auth.next = { role: null, state: 'pending' };
      expect(path(await run(authGuard))).toBe('/registration-pending');
      auth.next = { role: null, state: 'rejected' };
      expect(path(await run(authGuard))).toBe('/registration-pending');
    });
  });

  describe('adminGuard', () => {
    it('admits an admin', async () => {
      auth.next = { role: 'admin', state: 'registered' };
      expect(await run(adminGuard)).toBeTrue();
    });

    it('sends an instructor back to the dashboard', async () => {
      auth.next = { role: 'instructor', state: 'registered' };
      expect(path(await run(adminGuard))).toBe('/dashboard');
    });

    it('sends an anonymous visitor back to the dashboard (authGuard runs first in routes)', async () => {
      expect(path(await run(adminGuard))).toBe('/dashboard');
    });
  });
});
