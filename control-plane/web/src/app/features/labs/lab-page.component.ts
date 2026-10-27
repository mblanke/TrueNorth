import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { interval } from 'rxjs';

import { AuthService } from '@core/services/auth.service';
import { CourseStudioApiService, LabConsole, LabSession } from '@core/services/course-studio-api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeCredentialsComponent } from '../../shared/components/range-credentials/range-credentials.component';

/** What each session state means to the student. */
const STATE_TEXT: Record<string, string> = {
  queued: 'Waiting for a free lab. It starts automatically.',
  provisioning: 'Building your lab…',
  baselining: 'Almost ready: saving a reset point…',
  ready: 'Your lab is ready.',
  active: 'Your lab is running.',
  resetting: 'Resetting your lab to its starting point…',
  completed: 'You ended this lab.',
  expired: 'This lab timed out.',
  cleaning: 'Shutting the lab down…',
  destroyed: 'This lab has been shut down.',
  failed: 'This lab could not be built.',
  reconcile_required: 'Shutting the lab down…',
};
const WAITING = new Set(['queued', 'provisioning', 'baselining', 'resetting', 'cleaning', 'reconcile_required']);
const USABLE = new Set(['ready', 'active']);
const ENDED_STATES = new Set(['expired', 'cleaning', 'reconcile_required', 'destroyed']);
const TOKEN_KEY = (id: string) => `tn-lab-token:${id}`;

/**
 * A student's lab. Reached two ways: from a Moodle "Start lab" link (the LTI launch puts a
 * token for this one session in the URL fragment; it is moved to session storage and
 * removed from the address bar), or signed in to TrueNorth.
 */
@Component({
  selector: 'tn-lab-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  // Real use of the page (ADR 0014 §18): only this keeps an idle lab from closing.
  host: {
    '(window:keydown)': 'markActive()',
    '(window:pointerdown)': 'markActive()',
    '(window:focus)': 'markActive()',
  },
  imports: [DatePipe, MatButtonModule, MatCardModule, MatIconModule, MatProgressBarModule, RangeCredentialsComponent],
  template: `
    <div class="lab">
      <mat-card class="panel">
        <h1>Lab</h1>
        @if (session(); as s) {
          <p class="state" [class.bad]="s.state === 'failed'">{{ text(s.state, s.end_reason) }}</p>
          @if (waiting()) {
            <mat-progress-bar mode="indeterminate" />
          }
          @if (s.error) {
            <p [class.error]="s.state === 'failed'" [class.muted]="s.state !== 'failed'">{{ s.error }}</p>
          }
          @if (s.state === 'queued') {
            <div class="buttons">
              <button mat-stroked-button (click)="act('end')"><mat-icon>close</mat-icon> Leave the queue</button>
            </div>
          }
          @if (usable()) {
            <div class="buttons">
              <button mat-raised-button color="primary" (click)="openConsole()">
                <mat-icon>terminal</mat-icon> Open console
              </button>
              <button mat-stroked-button (click)="act('reset')"><mat-icon>restart_alt</mat-icon> Reset</button>
              <button mat-stroked-button color="warn" (click)="act('end')"><mat-icon>stop_circle</mat-icon> End lab</button>
            </div>
            @if (console(); as c) {
              <p class="console">
                Console ticket for <b>{{ c.node }}</b> (valid {{ c.expires_in }}s):
                <code>{{ c.url }}</code>
              </p>
            }
            <p class="muted">
              Ends {{ s.max_expires_at | date: 'shortTime' }} at the latest; closes after inactivity
              ({{ s.idle_expires_at | date: 'shortTime' }}). Your submitted work is kept when it ends.
            </p>
            <!-- This lab's own logins (ADR 0013 §3): new after every reset; each reveal is recorded. -->
            <tn-range-credentials [labSessionId]="sessionId" [labToken]="labToken" />
          }
          <p class="muted small">Attempt {{ s.attempt }} · {{ s.activity_id }}</p>
        } @else if (problem()) {
          <p class="error">{{ problem() }}</p>
        } @else {
          <mat-progress-bar mode="indeterminate" />
        }
      </mat-card>
    </div>
  `,
  styles: [`
    .lab { display: flex; justify-content: center; padding: 48px 16px; }
    .panel { width: min(640px, 100%); padding: 24px; border: 1px solid var(--border); display: flex; flex-direction: column; gap: 12px; }
    h1 { margin: 0; font-size: 22px; }
    .state { font-size: 1.05rem; margin: 0; }
    .state.bad, .error { color: var(--severity-high); }
    .buttons { display: flex; gap: 8px; flex-wrap: wrap; }
    .console code { display: block; word-break: break-all; font-size: 0.8rem; margin-top: 4px; }
    .muted { color: var(--text-muted); margin: 0; }
    .small { font-size: 0.8rem; }
  `],
})
export class LabPageComponent implements OnInit {
  private readonly api = inject(CourseStudioApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  readonly session = signal<LabSession | null>(null);
  readonly console = signal<LabConsole | null>(null);
  readonly problem = signal('');
  readonly waiting = computed(() => WAITING.has(this.session()?.state ?? ''));
  readonly usable = computed(() => USABLE.has(this.session()?.state ?? ''));
  private id = '';
  private interacted = false;
  private token: string | undefined;
  /** For the credentials panel: this session, and its LTI token when it came with one. */
  sessionId = '';
  labToken: string | undefined;

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
    this.token = this.takeToken();
    this.sessionId = this.id;
    this.labToken = this.token;
    if (this.token) {
      this.start();
      return;
    }
    // Signed-in route: the page has no guard, so resolve the session first.
    void this.auth.bootstrap().then(() => {
      if (this.auth.isAuthenticated()) {
        this.start();
      } else {
        void this.router.navigate(['/login'], { queryParams: { returnUrl: `/labs/${this.id}` } });
      }
    });
  }

  private start(): void {
    this.refresh();
    interval(5000).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      if (this.waiting()) {
        this.refresh();
      }
    });
    interval(60000).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      if (this.usable()) {
        // On an error (the lab ended, the enrollment closed) reload, so the page says so.
        // Says whether the Student used the page since the last beat; an open tab nobody
        // uses does not keep the lab open.
        const active = this.interacted;
        this.interacted = false;
        this.api.labHeartbeat(this.id, active, this.token).subscribe({
          next: s => this.session.set(s),
          error: () => this.refresh(),
        });
      }
    });
  }

  /** Input, focus or a console opened since the last heartbeat. */
  markActive(): void {
    this.interacted = true;
  }

  text(state: string, endReason = ''): string {
    // An idle close (ADR 0014 §18) is recorded as such; the lab ended, not the Student.
    if (endReason === 'idle' && ENDED_STATES.has(state)) {
      return 'This lab was closed because it was not used for a while. Your submitted work is kept.';
    }
    return STATE_TEXT[state] ?? state;
  }

  refresh(): void {
    this.api.lab(this.id, this.token).subscribe({
      next: s => this.session.set(s),
      error: err => this.problem.set(err?.status === 401 ? 'This lab link has expired. Open the lab again from your course.' : 'This lab could not be found.'),
    });
  }

  act(action: 'reset' | 'end'): void {
    const question = action === 'reset'
      ? 'Reset the lab to its starting point? Changes you made in it are lost.'
      : 'End the lab? Its machines are shut down; work you submitted is kept.';
    if (!window.confirm(question)) {
      return;
    }
    this.console.set(null);
    this.api.labAction(this.id, action, this.token).subscribe({
      next: s => this.session.set(s),
      error: err => this.notify.error(err?.error?.detail ?? `Could not ${action} the lab`),
    });
  }

  openConsole(): void {
    this.markActive();
    this.api.labConsole(this.id, this.token).subscribe({
      next: c => this.console.set(c),
      error: err => this.notify.error(err?.error?.detail ?? 'Could not open the console'),
    });
  }

  /** The launch token arrives in the fragment (#token=...); keep it for this tab only and
   *  take it out of the address bar so it is not bookmarked or shared. */
  private takeToken(): string | undefined {
    const fragment = this.route.snapshot.fragment ?? '';
    const match = /(?:^|&)token=([^&]+)/.exec(fragment);
    try {
      if (match) {
        sessionStorage.setItem(TOKEN_KEY(this.id), match[1]);
        history.replaceState(history.state, '', location.pathname + location.search);
        return match[1];
      }
      return sessionStorage.getItem(TOKEN_KEY(this.id)) ?? undefined;
    } catch {
      return match?.[1];
    }
  }
}
