import { Component, computed, effect, inject, untracked } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { RouterLink } from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import { EMAIL_DOCS_URL, EmailStatusService } from '@core/services/email-status.service';

/**
 * Persistent admin banner while the API cannot send email (#306). No dismiss: it stays
 * until the relay is set (install/README.md "Email (SMTP)", summarised in the app at
 * /admin/help/email) and the api restarted.
 * Admins only; asks the server once per session, once the signed-in user is known.
 */
@Component({
  selector: 'tn-email-banner',
  imports: [MatIconModule, RouterLink],
  template: `
    @if (show()) {
      <div class="email-banner" role="alert">
        <mat-icon>mail_lock</mat-icon>
        <span>
          Email is not configured: onboarding emails, reminders and calendar invites are not being sent.
          <a [routerLink]="docsUrl">Set up email</a>
        </span>
      </div>
    }
  `,
  styles: [`
    .email-banner {
      display: flex; align-items: center; gap: 12px; margin: 0 0 16px; padding: 12px 16px;
      border-radius: 8px; background: var(--mat-sys-error-container); color: var(--mat-sys-on-error-container);
    }
    .email-banner span { flex: 1; }
    .email-banner a { color: inherit; font-weight: 600; margin-left: 4px; }
  `],
})
export class EmailBannerComponent {
  private readonly auth = inject(AuthService);
  private readonly email = inject(EmailStatusService);
  readonly docsUrl = EMAIL_DOCS_URL;
  readonly show = computed(() => this.auth.isAdmin() && this.email.notConfigured());

  constructor() {
    effect(() => {
      if (this.auth.isAdmin()) untracked(() => this.email.load());
    });
  }
}
