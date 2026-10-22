import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Clipboard } from '@angular/cdk/clipboard';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { roleCan } from '@core/auth/permissions';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CredentialSource, RangeCredential, RangeCredentialsApiService,
} from '@core/services/range-credentials-api.service';

const MASK = '••••••••••';

/**
 * A range's machine accounts (ADR 0012 §13, ADR 0013 §3), for the range detail page
 * (staff), a Student's exercise view and a Student's own lab page (`labSessionId`, with the
 * lab token after a Moodle launch). What it lists is the API's decision, never filtered
 * here: staff (`range:credentials_read`) get every account, a Student only the accounts
 * handed to them. Observers are not shown the panel (the API would answer 403).
 *
 * The list carries no password. Each password is fetched only when the person reveals or
 * copies it (`POST .../reveal`, one audit row per reveal) and is forgotten when hidden.
 * Admins (`range:credentials_rotate`) can give an account a new password, with a reason.
 */
@Component({
  selector: 'tn-range-credentials',
  imports: [MatButtonModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (shown()) {
      <section class="creds" [attr.aria-label]="title()">
        <header class="head">
          <h3><mat-icon>key</mat-icon> {{ title() }}</h3>
          @if (accounts() === null && !loading()) {
            <button mat-stroked-button (click)="load()"><mat-icon>visibility</mat-icon> Show accounts</button>
          }
        </header>
        <p class="muted">
          @if (staff()) {
            Every account on this range's machines. Passwords are generated for this range; each one you reveal is recorded in the audit log.
          } @else {
            The accounts handed to you for these machines. They change when the range is reset or rebuilt.
          }
        </p>

        @if (loading()) {
          <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading accounts"></div>
        }
        @if (error()) {
          <p class="muted" role="status">{{ error() }}</p>
        }
        @if (accounts(); as list) {
          @if (list.length === 0) {
            <p class="muted" role="status">
              {{ staff() ? 'This range has no accounts (its machines are appliances).' : 'No accounts are handed to you here yet.' }}
            </p>
          } @else {
            <table class="tn-table">
              <caption class="sr-only">{{ title() }}</caption>
              <thead>
                <tr>
                  <th scope="col">Machine</th><th scope="col">Username</th><th scope="col">Password</th>
                  @if (staff()) { <th scope="col">Role</th><th scope="col">Students</th> }
                  <th scope="col">Notes</th>
                </tr>
              </thead>
              <tbody>
                @for (a of list; track key(a)) {
                  <tr>
                    <td>
                      <code>{{ a.machine }}</code>
                      @if (staff() && a.source === 'implicit') {
                        <span class="badge info" matTooltip="A built-in login of the machine's golden template. TrueNorth gives it a password generated for this range, so the template password no longer works.">
                          Staff login
                        </span>
                      }
                      @if (staff() && a.scenario) {
                        <span class="badge info" matTooltip="Typed into the range template on purpose: the weak password is part of the exercise. It is never rotated.">
                          Scenario
                        </span>
                      }
                      @if (staff() && a.pending_change) {
                        <span class="badge info" matTooltip="A new password is being set on the machine. Until it is, a reveal shows the password the machine still has.">
                          Change pending
                        </span>
                      }
                      @if (staff() && a.setup_status === 'being_set_up') {
                        <span class="badge setup" matTooltip="This machine's deploy-time setup (a product role image or Security Onion) is still running. TrueNorth verifies and records the account once it finishes; Students see it only then.">
                          Being set up
                        </span>
                      } @else if (staff() && !a.applied) {
                        <span class="badge" matTooltip="The last build did not set this account (or the range is not built): Windows accounts other than Administrator, appliances and other backends are scenario content. Students are not shown it.">
                          Not set by TrueNorth
                        </span>
                      }
                    </td>
                    <td>
                      <code>{{ a.username }}</code>
                      <button mat-icon-button (click)="copy(a.username, 'Username')" matTooltip="Copy username"
                              [attr.aria-label]="'Copy username ' + a.username + ' on ' + a.machine">
                        <mat-icon>content_copy</mat-icon>
                      </button>
                    </td>
                    <td class="pw">
                      <code class="secret" [attr.data-revealed]="isRevealed(a)">{{ passwordOf(a) ?? mask }}</code>
                      <button mat-icon-button (click)="toggle(a)" [disabled]="busy() === a.id"
                              [matTooltip]="isRevealed(a) ? 'Hide password' : 'Show password (recorded in the audit log)'"
                              [attr.aria-pressed]="isRevealed(a)"
                              [attr.aria-label]="(isRevealed(a) ? 'Hide' : 'Show') + ' password of ' + a.username + ' on ' + a.machine">
                        <mat-icon>{{ isRevealed(a) ? 'visibility_off' : 'visibility' }}</mat-icon>
                      </button>
                      <button mat-icon-button (click)="copyPassword(a)" [disabled]="busy() === a.id" matTooltip="Copy password"
                              [attr.aria-label]="'Copy password of ' + a.username + ' on ' + a.machine">
                        <mat-icon>content_copy</mat-icon>
                      </button>
                      @if (canRotate() && a.generated && !a.scenario && !a.pending_change) {
                        <button mat-icon-button (click)="rotate(a)" [disabled]="busy() === a.id" matTooltip="Give it a new password"
                                [attr.aria-label]="'Rotate password of ' + a.username + ' on ' + a.machine">
                          <mat-icon>autorenew</mat-icon>
                        </button>
                      }
                    </td>
                    @if (staff()) {
                      <td>{{ a.role }}</td>
                      <td>{{ a.student_visible ? 'Handed out' : '—' }}</td>
                    }
                    <td class="notes">{{ a.notes }}@if (staff() && a.generated) { <span class="muted"> (generated)</span> }</td>
                  </tr>
                }
              </tbody>
            </table>
          }
        }
      </section>
    }
  `,
  styles: [`
    .creds { display: block; }
    .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    h3 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 16px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    .pw { white-space: nowrap; }
    .secret { display: inline-block; min-width: 11ch; }
    .notes { font-size: 13px; }
    .badge { display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 8px; font-size: 11px;
      border: 1px solid var(--warning, #f59e0b); color: var(--text-secondary); white-space: nowrap; }
    .badge.setup, .badge.info { border-color: var(--info, #3b82f6); }
    code { font-family: var(--font-mono); font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeCredentialsComponent {
  private api = inject(RangeCredentialsApiService);
  private auth = inject(AuthService);
  private clipboard = inject(Clipboard);
  private notify = inject(NotificationService);

  /** A range's accounts (range detail, exercise view)... */
  readonly rangeId = input<string>();
  /** ...or a Student's own lab session's (the lab page), with its token after an LTI launch. */
  readonly labSessionId = input<string>();
  readonly labToken = input<string>();

  readonly mask = MASK;
  readonly source = computed<CredentialSource | null>(() => {
    const lab = this.labSessionId();
    if (lab) return { kind: 'lab', sessionId: lab, token: this.labToken() || undefined };
    const range = this.rangeId();
    return range ? { kind: 'range', rangeId: range } : null;
  });
  private readonly isLab = computed(() => this.source()?.kind === 'lab');
  readonly staff = computed(() => !this.isLab() && roleCan(this.auth.user()?.role, 'range:credentials_read'));
  readonly canRotate = computed(() => !this.isLab() && roleCan(this.auth.user()?.role, 'range:credentials_rotate'));
  /** Staff and Students (a lab page is its Student's); an observer would only be refused. */
  readonly shown = computed(() => this.source() !== null
    && (this.isLab() || this.staff() || this.auth.user()?.role === 'student'));
  readonly title = computed(() => (this.staff() ? 'Credentials' : 'Your accounts'));

  readonly accounts = signal<RangeCredential[] | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  /** The account a reveal or rotation is in flight for. */
  readonly busy = signal<string | null>(null);
  /** Revealed passwords by account id, in memory only, forgotten when hidden. */
  private readonly revealed = signal<ReadonlyMap<string, string>>(new Map());

  constructor() {
    // Another range or lab: forget the previous one's accounts (they were for it only).
    effect(() => {
      this.source();
      untracked(() => this.reset());
    });
  }

  key(a: RangeCredential): string {
    return a.id;
  }

  load(): void {
    const source = this.source();
    if (!source) return;
    this.loading.set(true);
    this.error.set('');
    this.api.list(source).subscribe({
      next: res => {
        if (source !== this.source()) return;
        this.accounts.set(res.accounts);
        this.loading.set(false);
      },
      error: (err: unknown) => {
        if (source !== this.source()) return;
        this.loading.set(false);
        this.error.set(describe(err, this.staff()));
      },
    });
  }

  isRevealed(a: RangeCredential): boolean {
    return this.revealed().has(a.id);
  }

  passwordOf(a: RangeCredential): string | undefined {
    return this.revealed().get(a.id);
  }

  /** Show (one audited reveal) or hide (forget) one account's password. */
  toggle(a: RangeCredential): void {
    if (this.isRevealed(a)) {
      this.forget(a.id);
      return;
    }
    this.fetch(a, pw => this.remember(a.id, pw));
  }

  /** Copy a password, revealing it first (audited) when it is not shown. */
  copyPassword(a: RangeCredential): void {
    const shown = this.passwordOf(a);
    if (shown !== undefined) {
      this.copy(shown, 'Password');
      return;
    }
    this.fetch(a, pw => this.copy(pw, 'Password'));
  }

  copy(text: string, what: 'Username' | 'Password'): void {
    if (this.clipboard.copy(text)) {
      this.notify.success(`${what} copied`);
    } else {
      this.notify.error(`${what} could not be copied`);
    }
  }

  /** Admin: a new generated password for the account, with a reason for the audit log. */
  rotate(a: RangeCredential): void {
    const source = this.source();
    if (!source || source.kind !== 'range') return;
    const reason = (window.prompt(`Why give ${a.username} on ${a.machine} a new password? (recorded in the audit log)`)
      ?? '').trim();
    if (reason.length < 3) return;
    this.busy.set(a.id);
    this.api.rotate(source.rangeId, a.id, reason).subscribe({
      next: res => {
        this.busy.set(null);
        this.forget(a.id);
        this.accounts.update(list => (list ?? []).map(x => (x.id === a.id ? res.account : x)));
        this.notify.success(res.applies === 'in_guest'
          ? (res.dispatched ? 'New password issued; it is being set on the machine'
            : 'New password issued, but the task queue was not reachable: rotate again later')
          : 'New password issued; the next build sets it');
      },
      error: (err: unknown) => {
        this.busy.set(null);
        this.notify.error(describe(err, this.staff()));
      },
    });
  }

  private fetch(a: RangeCredential, then: (password: string) => void): void {
    const source = this.source();
    if (!source) return;
    this.busy.set(a.id);
    this.api.reveal(source, a.id).subscribe({
      next: res => {
        this.busy.set(null);
        if (source === this.source()) then(res.password);
      },
      error: (err: unknown) => {
        this.busy.set(null);
        this.notify.error(describe(err, this.staff()));
      },
    });
  }

  private remember(id: string, password: string): void {
    const next = new Map(this.revealed());
    next.set(id, password);
    this.revealed.set(next);
  }

  private forget(id: string): void {
    const next = new Map(this.revealed());
    next.delete(id);
    this.revealed.set(next);
  }

  private reset(): void {
    this.accounts.set(null);
    this.error.set('');
    this.loading.set(false);
    this.busy.set(null);
    this.revealed.set(new Map());
  }
}

/** What a refusal means to the person looking, in the API's terms. */
function describe(err: unknown, staff: boolean): string {
  if (err instanceof HttpErrorResponse) {
    if (err.status === 404) {
      return staff ? 'Range not found (or not in your organisation).' : 'No accounts are handed to you on this range.';
    }
    if (err.status === 403) return 'Your role does not see range credentials.';
    if (err.status === 0) return 'The API could not be reached.';
    const detail = err.error?.detail;
    if (typeof detail === 'string') return detail;
    return `The API answered ${err.status}.`;
  }
  return 'The accounts could not be loaded.';
}
