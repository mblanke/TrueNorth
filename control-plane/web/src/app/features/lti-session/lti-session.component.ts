import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { firstValueFrom } from 'rxjs';

import { handoffCode, storeLtiSession } from '@core/auth/lti-session';
import { safeReturnUrl } from '@core/auth/return-url';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';

/**
 * Where an LMS launch lands a Student who has no TrueNorth sign-in of their own
 * (`/lti/session#code=…`, set by POST /lti/launch). The code is taken out of the address
 * bar at once, exchanged once for a TrueNorth session, and the Student goes on to what
 * they launched (a quiz, an exercise, a course). No guard: this page is how they sign in.
 */
@Component({
  selector: 'tn-lti-session',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatCardModule, MatProgressBarModule],
  template: `
    <div class="lti-session">
      <mat-card class="panel">
        @if (error()) {
          <h1>Could not open this activity</h1>
          <p class="error">{{ error() }}</p>
          <p>Go back to your course in the learning platform and open the activity again.</p>
        } @else {
          <h1>Opening your activity…</h1>
          <mat-progress-bar mode="indeterminate" />
        }
      </mat-card>
    </div>
  `,
  styles: [`
    .lti-session { display: flex; justify-content: center; padding: 64px 16px; }
    .panel { max-width: 520px; width: 100%; padding: 24px; }
    .error { color: var(--alert, #ef4444); }
  `],
})
export class LtiSessionComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  readonly error = signal('');

  async ngOnInit(): Promise<void> {
    const code = handoffCode(this.route.snapshot.fragment);
    // The code is a credential: out of the address bar and history before anything else.
    history.replaceState(history.state, '', location.pathname + location.search);
    if (!code) {
      this.error.set('This link has no sign-in code.');
      return;
    }
    try {
      // A safe request first, so the API's CSRF cookie exists for the POST that follows.
      await firstValueFrom(this.api.health()).catch(() => undefined);
      const session = await firstValueFrom(this.api.exchangeLtiSession(code));
      storeLtiSession(session.access_token, session.expires_in);
      await this.auth.bootstrap(true);
      await this.router.navigateByUrl(safeReturnUrl(session.target) ?? '/dashboard', { replaceUrl: true });
    } catch (err: unknown) {
      const status = (err as { status?: number })?.status;
      this.error.set(
        status === 401
          ? 'This sign-in link has been used, has expired, or was opened in another browser.'
          : 'TrueNorth could not be reached.',
      );
    }
  }
}
