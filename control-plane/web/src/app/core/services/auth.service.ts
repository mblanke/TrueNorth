import { Injectable, signal, computed } from '@angular/core';
import { environment } from '@env/environment';

export interface CurrentUser {
  sub: string;
  email: string;
  display_name: string;
  role: 'admin' | 'instructor' | 'trainee';
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private userSignal = signal<CurrentUser | null>(null);

  readonly user = this.userSignal.asReadonly();
  readonly isAuthenticated = computed(() => this.userSignal() !== null);
  readonly isAdmin = computed(() => this.userSignal()?.role === 'admin');
  readonly isInstructor = computed(() => {
    const role = this.userSignal()?.role;
    return role === 'admin' || role === 'instructor';
  });

  constructor() {
    if (environment.authDisabled) {
      this.userSignal.set({
        sub: 'dev-user-001',
        email: 'admin@truenorth.local',
        display_name: 'Dev Admin',
        role: 'admin',
      });
    }
  }

  login(): void {
    // In production, redirect to Keycloak
    if (!environment.authDisabled) {
      window.location.href = `${environment.keycloak.url}/realms/${environment.keycloak.realm}/protocol/openid-connect/auth?client_id=${environment.keycloak.clientId}&redirect_uri=${window.location.origin}&response_type=code&scope=openid`;
    }
  }

  logout(): void {
    this.userSignal.set(null);
    if (!environment.authDisabled) {
      window.location.href = `${environment.keycloak.url}/realms/${environment.keycloak.realm}/protocol/openid-connect/logout?redirect_uri=${window.location.origin}`;
    }
  }

  setUser(user: CurrentUser): void {
    this.userSignal.set(user);
  }
}
