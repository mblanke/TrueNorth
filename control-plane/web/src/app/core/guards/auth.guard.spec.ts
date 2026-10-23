import { TestBed } from '@angular/core/testing';
import { computed, signal } from '@angular/core';
import { ActivatedRouteSnapshot, Router, RouterStateSnapshot, UrlTree, provideRouter } from '@angular/router';
import { Observable, firstValueFrom } from 'rxjs';
import { AuthService, AuthState, UserRole } from '../services/auth.service';
import { adminGuard, authGuard, demoGuard, pageGuard, rulesOfUseGuard } from './auth.guard';
import { PAGE_ACCESS, PageKey, roleHasAccess } from '../auth/permissions';
import { environment } from '@env/environment';

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
  canOpen(page: PageKey): boolean {
    return roleHasAccess(this.role(), PAGE_ACCESS[page]);
  }
  private readonly blocked = signal(false);
  readonly mfaBlocked = this.blocked.asReadonly();
  private readonly ack = signal(false);
  readonly acknowledgementRequired = this.ack.asReadonly();

  next: { role: UserRole | null; state: AuthState | null; mfaBlocked?: boolean; acknowledgement?: boolean } = {
    role: null,
    state: null,
  };
  readonly bootstrap = jasmine.createSpy('bootstrap').and.callFake(async () => {
    this.role.set(this.next.role);
    this.state.set(this.next.state);
    this.blocked.set(!!this.next.mfaBlocked);
    this.ack.set(!!this.next.acknowledgement);
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

  function run(guard: typeof authGuard, url?: string): Promise<boolean | UrlTree> {
    const state = (url === undefined ? {} : { url }) as RouterStateSnapshot;
    const result = TestBed.runInInjectionContext(() =>
      guard({} as ActivatedRouteSnapshot, state),
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

    it('sends an account still lacking MFA after a step-up to /mfa-required', async () => {
      auth.next = { role: null, state: null, mfaBlocked: true };
      expect(path(await run(authGuard))).toBe('/mfa-required');
    });

    it('sends a user who must sign the rules of use there first, keeping where they were going', async () => {
      auth.next = { role: 'student', state: 'registered', acknowledgement: true };
      expect(path(await run(authGuard, '/scoring?exercise=ex1'))).toBe(
        '/rules-of-use?returnUrl=%2Fscoring%3Fexercise%3Dex1',
      );
    });

    it('asks for MFA before the rules of use', async () => {
      auth.next = { role: null, state: null, mfaBlocked: true, acknowledgement: true };
      expect(path(await run(authGuard))).toBe('/mfa-required');
    });

    it('sends an anonymous visitor to /login', async () => {
      expect(path(await run(authGuard))).toBe('/login');
    });

    it('passes the attempted URL to /login as returnUrl', async () => {
      const result = await run(authGuard, '/scoring?exercise=ex1');
      expect(result instanceof UrlTree).toBeTrue();
      const tree = result as UrlTree;
      expect(tree.root.children['primary']?.segments.map(s => s.path)).toEqual(['login']);
      expect(tree.queryParams).toEqual({ returnUrl: '/scoring?exercise=ex1' });
    });

    it('adds no returnUrl for the root', async () => {
      expect(path(await run(authGuard, '/'))).toBe('/login');
    });

    for (const unsafe of ['//evil.test/x', '/\\evil.test', 'https://evil.test/']) {
      it(`drops an unsafe attempted URL (${unsafe})`, async () => {
        expect(path(await run(authGuard, unsafe))).toBe('/login');
      });
    }

    it('does not add returnUrl when redirecting to /register', async () => {
      auth.next = { role: null, state: 'unregistered' };
      expect(path(await run(authGuard, '/labs/l1'))).toBe('/register');
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

  /**
   * Deep links (R0-B). Each row: a role, a page, and whether the guard lets it in. The
   * page table is the one the menu reads, and it mirrors rbac.py, so "allowed" here
   * means the page's API calls would answer the role too.
   */
  describe('pageGuard', () => {
    const cases: [UserRole, PageKey, boolean][] = [
      ['student', 'scoring', true],          // their own AARs (aar:read; scoped by the API)
      ['student', 'telemetry', false],
      ['student', 'opsCenter', false],
      ['student', 'authoring', false],
      ['student', 'approvals', false],
      ['student', 'infrastructure', false],
      ['student', 'support', true],
      ['observer', 'opsCenter', true],
      ['observer', 'scoring', true],
      ['observer', 'telemetry', true],
      ['observer', 'authoring', false],
      ['observer', 'support', false],         // no ticket:create
      ['range_ops', 'authoring', true],
      ['range_ops', 'authoringRanges', true],
      ['range_ops', 'authoringContent', true],
      ['range_ops', 'authoringScenarios', false],
      ['range_ops', 'authoringCourses', false],
      ['range_ops', 'scoring', false],        // no aar:read
      ['range_ops', 'approvals', false],
      ['instructor', 'approvals', true],
      ['instructor', 'authoringCourses', true],
      ['instructor', 'telemetry', true],
      ['admin', 'approvals', true],
    ];
    for (const [role, page, allowed] of cases) {
      it(`${allowed ? 'admits' : 'refuses'} ${role} on ${page}`, async () => {
        auth.next = { role, state: 'registered' };
        const result = path(await run(pageGuard(page)));
        expect(result).toBe(allowed ? true : '/dashboard');
        expect(auth.bootstrap).toHaveBeenCalled();
      });
    }

    it('refuses an anonymous visitor', async () => {
      expect(path(await run(pageGuard('scoring')))).toBe('/dashboard');
    });
  });

  describe('demoGuard (fictional previews)', () => {
    const configured = environment.demoPreviews;
    afterEach(() => (environment.demoPreviews = configured));

    it('sends everyone back to Learning by default', () => {
      environment.demoPreviews = undefined;
      const result = TestBed.runInInjectionContext(() =>
        demoGuard({} as ActivatedRouteSnapshot, {} as RouterStateSnapshot)) as boolean | UrlTree;
      expect(path(result)).toBe('/learning');
    });

    it('admits when the deployment turned the demo on', () => {
      environment.demoPreviews = true;
      const result = TestBed.runInInjectionContext(() =>
        demoGuard({} as ActivatedRouteSnapshot, {} as RouterStateSnapshot)) as boolean | UrlTree;
      expect(result).toBeTrue();
    });
  });

  describe('rulesOfUseGuard', () => {
    it('admits anyone signed in, whether or not they must sign', async () => {
      auth.next = { role: 'student', state: 'registered', acknowledgement: true };
      expect(await run(rulesOfUseGuard)).toBeTrue();
      auth.next = { role: 'admin', state: 'registered', acknowledgement: false };
      expect(await run(rulesOfUseGuard)).toBeTrue();
    });

    it('sends someone without an account where authGuard would', async () => {
      auth.next = { role: null, state: 'unregistered' };
      expect(path(await run(rulesOfUseGuard))).toBe('/register');
      auth.next = { role: null, state: null };
      expect(path(await run(rulesOfUseGuard))).toBe('/login');
    });
  });
});
