import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { KeycloakService } from 'keycloak-angular';
import { ReplaySubject, firstValueFrom } from 'rxjs';
import { environment } from '@env/environment';

/**
 * Roles as the API defines them (`UserRole` in control-plane/api/app/models.py).
 *
 * This used to read `'admin' | 'instructor' | 'trainee'`. `trainee` does not
 * exist server-side — the backend enum is a native Postgres type baked into
 * ROLE_PERMISSIONS and the UserCreateIn regex, so the frontend is what moves.
 * "Trainee" survives as a display label; `student` is the wire value.
 */
export type UserRole = 'admin' | 'instructor' | 'student' | 'observer' | 'range_ops';

/** Where an authenticated identity sits relative to having an account. */
export type AuthState = 'unregistered' | 'pending' | 'rejected' | 'registered';

export interface CurrentUser {
  sub: string;
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  onboarding_state?: string;
}

export interface RegistrationPrefill {
  email: string;
  display_name: string;
  first_name: string | null;
  last_name: string | null;
  ad_object_guid: string | null;
  ad_distinguished_name: string | null;
}

export interface RegistrationSuggestions {
  role: UserRole | null;
  tenant_id: string | null;
  tenant_name: string | null;
  matched_groups: string[];
  may_register: boolean;
}

export interface AuthMe {
  status: AuthState;
  user?: Record<string, unknown> & { id: string; role: UserRole; onboarding_state?: string };
  request?: Record<string, unknown>;
  prefill?: RegistrationPrefill;
  suggestions?: RegistrationSuggestions;
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly keycloak = inject(KeycloakService);
  private readonly http = inject(HttpClient);

  private userSignal = signal<CurrentUser | null>(null);
  private stateSignal = signal<AuthState | null>(null);
  private meSignal = signal<AuthMe | null>(null);

  readonly user = this.userSignal.asReadonly();
  readonly authState = this.stateSignal.asReadonly();
  readonly me = this.meSignal.asReadonly();

  readonly isAuthenticated = computed(() => this.userSignal() !== null);
  readonly isAdmin = computed(() => this.userSignal()?.role === 'admin');
  readonly isInstructor = computed(() => {
    const role = this.userSignal()?.role;
    return role === 'admin' || role === 'instructor';
  });
  readonly userId = computed(() => this.userSignal()?.id ?? null);
  readonly onboardingState = computed(() => this.userSignal()?.onboarding_state ?? 'not_started');
  readonly needsOnboarding = computed(() => {
    // Only trainees are gated on first-run. Trapping an admin behind a profile
    // wizard on their first login is the wrong trade.
    const u = this.userSignal();
    return !!u && u.role === 'student' && this.onboardingState() !== 'complete';
  });

  /**
   * Emits once the initial identity resolution has finished, whatever the
   * outcome. Guards await this rather than reading a signal synchronously,
   * because `/auth/me` is a network call and a guard can run before it lands.
   */
  readonly ready$ = new ReplaySubject<boolean>(1);
  private bootstrapped = false;

  constructor() {
    if (environment.authDisabled) {
      this.userSignal.set({
        sub: 'dev-user-001',
        id: '00000000-0000-0000-0000-000000000001',
        email: 'admin@truenorth.local',
        display_name: 'Dev Admin',
        role: 'admin',
        onboarding_state: 'complete',
      });
      this.stateSignal.set('registered');
      this.bootstrapped = true;
      this.ready$.next(true);
    }
  }

  /**
   * Resolve identity state from the API.
   *
   * The DB row is the authority, not the token: `/auth/me` returns the real
   * `users.id`, the role actually assigned at approval, and onboarding
   * progress. The token only proves who the person is.
   */
  async bootstrap(force = false): Promise<AuthMe | null> {
    if (environment.authDisabled) {
      return this.meSignal();
    }
    if (this.bootstrapped && !force) {
      return this.meSignal();
    }

    if (!(await this.keycloak.isLoggedIn())) {
      this.userSignal.set(null);
      this.stateSignal.set(null);
      this.finishBootstrap();
      return null;
    }

    try {
      const me = await firstValueFrom(this.http.get<AuthMe>(`${environment.apiUrl}/auth/me`));
      this.meSignal.set(me);
      this.stateSignal.set(me.status);
      if (me.status === 'registered' && me.user) {
        this.userSignal.set({
          sub: this.keycloak.getKeycloakInstance().subject ?? '',
          id: String(me.user['id']),
          email: String(me.user['email'] ?? ''),
          display_name: String(me.user['display_name'] ?? ''),
          role: me.user.role,
          onboarding_state: me.user.onboarding_state ?? 'not_started',
        });
      } else {
        this.userSignal.set(null);
      }
      this.finishBootstrap();
      return me;
    } catch {
      // A valid token with an unreachable API is not the same as being logged
      // out — leave the user unset and let the caller surface the failure.
      this.userSignal.set(null);
      this.stateSignal.set(null);
      this.finishBootstrap();
      return null;
    }
  }

  private finishBootstrap(): void {
    this.bootstrapped = true;
    this.ready$.next(true);
  }

  login(redirectUri?: string): void {
    if (environment.authDisabled) {
      return;
    }
    void this.keycloak.login({ redirectUri: redirectUri ?? window.location.origin });
  }

  logout(): void {
    this.userSignal.set(null);
    this.stateSignal.set(null);
    this.meSignal.set(null);
    this.bootstrapped = false;
    if (environment.authDisabled) {
      return;
    }
    void this.keycloak.logout(window.location.origin);
  }

  getToken(): Promise<string> {
    return environment.authDisabled ? Promise.resolve('') : this.keycloak.getToken();
  }

  /** Test seam and dev-mode helper. */
  setUser(user: CurrentUser): void {
    this.userSignal.set(user);
    this.stateSignal.set('registered');
  }
}
