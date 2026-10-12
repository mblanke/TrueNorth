import { Component, inject } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import Keycloak from 'keycloak-js';
import { AuthService } from '@core/services/auth.service';
import { STEP_UP_ACR, clearMfaStepUp } from '@core/auth/mfa-step-up';

/**
 * Shown when the API still wants a second factor after one step-up (the loop guard in
 * core/auth/mfa-step-up.ts): the account could not complete multi-factor sign-in, and
 * someone has to look at it (docs/runbooks/break-glass.md).
 */
@Component({
  selector: 'tn-mfa-required',
  imports: [MatCardModule, MatButtonModule, MatIconModule],
  template: `
    <div class="shell">
      <mat-card class="card">
        <mat-icon class="glyph">phonelink_lock</mat-icon>
        <h2>Multi-factor sign-in required</h2>
        <p class="muted">
          Your role needs a second factor (an authenticator app code), and this sign-in did not
          provide one. If you were not asked to set up an authenticator app, or you have lost
          yours, contact your administrator: they can reset it so you can enrol again.
        </p>
        <div class="actions">
          <button mat-flat-button color="primary" (click)="tryAgain()">Try again</button>
          <button mat-button (click)="signOut()">Sign out</button>
        </div>
      </mat-card>
    </div>
  `,
  styles: [
    `
      .shell {
        display: flex;
        justify-content: center;
        padding: 3rem 1rem;
      }
      .card {
        width: min(560px, 100%);
        text-align: center;
        padding: 2rem;
      }
      .glyph {
        font-size: 3rem;
        width: 3rem;
        height: 3rem;
        opacity: 0.7;
      }
      h2 {
        margin: 0.75rem 0 0.5rem;
      }
      .muted {
        opacity: 0.75;
        margin: 0 auto 1.25rem;
        max-width: 48ch;
      }
      .actions {
        display: flex;
        gap: 0.5rem;
        justify-content: center;
        flex-wrap: wrap;
      }
    `,
  ],
})
export class MfaRequiredComponent {
  private readonly auth = inject(AuthService);
  private readonly keycloak = inject(Keycloak);

  /** An explicit retry: the person chose it, so it is not a loop. */
  tryAgain(): void {
    clearMfaStepUp();
    void this.keycloak.login({ acr: STEP_UP_ACR, redirectUri: window.location.origin });
  }

  signOut(): void {
    clearMfaStepUp();
    this.auth.logout();
  }
}
