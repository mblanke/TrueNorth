import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { firstValueFrom } from 'rxjs';

import { handoffCode } from '@core/auth/lti-session';
import { ApiService, LtiLinkPreview } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';

const PENDING = 'tn.lti-link-code';

/**
 * Staff deep linking (control-plane/api/app/lti_identity/links.py): a learning platform
 * account whose email is a staff account's asks to deep-link, and the staff member links
 * it here, once, signed in to TrueNorth with their own sign-in, in the same browser.
 * Nothing is linked until they press Link. The code waits in this tab's sessionStorage
 * across the sign-in redirect, never in a URL handed to the identity provider.
 */
@Component({
  selector: 'tn-lti-link',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatCardModule, MatProgressBarModule],
  template: `
    <div class="lti-link">
      <mat-card class="panel">
        <h1>Link a learning-platform account</h1>
        @if (done()) {
          <p>Linked. Go back to your course and choose <em>Select content</em> again.</p>
        } @else if (error()) {
          <p class="error">{{ error() }}</p>
        } @else if (preview(); as p) {
          <p>
            Link the account <strong>{{ p.lms_name || 'without a name' }}</strong> on
            <strong>{{ p.platform_name }}</strong> to your TrueNorth account
            <strong>{{ me() }}</strong>?
          </p>
          <p class="muted">That account then acts as you when it launches TrueNorth. Link it only if it is yours.</p>
          <button mat-raised-button color="primary" [disabled]="busy()" (click)="confirm()">Link</button>
        } @else {
          <mat-progress-bar mode="indeterminate" />
        }
      </mat-card>
    </div>
  `,
  styles: [`
    .lti-link { display: flex; justify-content: center; padding: 64px 16px; }
    .panel { max-width: 560px; width: 100%; padding: 24px; }
    .error { color: var(--alert, #ef4444); }
    .muted { color: var(--text-secondary); font-size: 13px; }
  `],
})
export class LtiLinkComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly route = inject(ActivatedRoute);

  readonly preview = signal<LtiLinkPreview | null>(null);
  readonly error = signal('');
  readonly done = signal(false);
  readonly busy = signal(false);
  private code = '';

  me(): string {
    const u = this.auth.user();
    return u ? `${u.display_name} (${u.email})` : '';
  }

  async ngOnInit(): Promise<void> {
    const fresh = handoffCode(this.route.snapshot.fragment);
    history.replaceState(history.state, '', location.pathname + location.search);
    if (fresh) {
      sessionStorage.setItem(PENDING, fresh);
    }
    this.code = sessionStorage.getItem(PENDING) ?? '';
    if (!this.code) {
      this.error.set('This link has no code. Deep-link again from your course.');
      return;
    }
    await this.auth.bootstrap();
    if (!this.auth.isAuthenticated() || this.auth.isLtiSession()) {
      // Sign in as yourself first; the code waits in this tab.
      this.auth.login(`${window.location.origin}/lti/link`);
      return;
    }
    try {
      this.preview.set(await firstValueFrom(this.api.previewLtiLink(this.code)));
    } catch (err: unknown) {
      this.fail(err);
    }
  }

  async confirm(): Promise<void> {
    this.busy.set(true);
    try {
      await firstValueFrom(this.api.confirmLtiLink(this.code));
      sessionStorage.removeItem(PENDING);
      this.done.set(true);
    } catch (err: unknown) {
      this.fail(err);
    } finally {
      this.busy.set(false);
    }
  }

  private fail(err: unknown): void {
    sessionStorage.removeItem(PENDING);
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    this.error.set(typeof detail === 'string' ? detail : 'This link could not be completed.');
  }
}
