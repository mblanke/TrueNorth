import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Clipboard } from '@angular/cdk/clipboard';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { roleCan } from '@core/auth/permissions';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeCredential, RangeCredentialsApiService } from '@core/services/range-credentials-api.service';

const MASK = '••••••••••';

/**
 * A range's machine accounts (ADR 0012 §13), for the range detail page (staff) and a
 * Student's exercise view. What it lists is the API's decision, never filtered here:
 * staff (`range:credentials_read`) get every account, a Student only the accounts handed
 * to them on a range they take part in. Observers are not shown the panel (the API
 * would answer 403).
 *
 * Nothing is fetched until the person asks (each read is audited), passwords stay masked
 * until revealed one at a time, and copy works without revealing.
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
            Every account on this range's machines, from its template. Each time they are shown is recorded in the audit log.
          } @else {
            The accounts handed to you for this range's machines.
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
              {{ staff() ? "This range's template declares no accounts." : 'No accounts are handed to you on this range.' }}
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
                      @if (staff() && !a.applied) {
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
                      <code class="secret" [attr.data-revealed]="isRevealed(a)">{{ isRevealed(a) ? a.password : mask }}</code>
                      <button mat-icon-button (click)="toggle(a)" [matTooltip]="isRevealed(a) ? 'Hide password' : 'Show password'"
                              [attr.aria-pressed]="isRevealed(a)"
                              [attr.aria-label]="(isRevealed(a) ? 'Hide' : 'Show') + ' password of ' + a.username + ' on ' + a.machine">
                        <mat-icon>{{ isRevealed(a) ? 'visibility_off' : 'visibility' }}</mat-icon>
                      </button>
                      <button mat-icon-button (click)="copy(a.password, 'Password')" matTooltip="Copy password"
                              [attr.aria-label]="'Copy password of ' + a.username + ' on ' + a.machine">
                        <mat-icon>content_copy</mat-icon>
                      </button>
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
    code { font-family: var(--font-mono); font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeCredentialsComponent {
  private api = inject(RangeCredentialsApiService);
  private auth = inject(AuthService);
  private clipboard = inject(Clipboard);
  private notify = inject(NotificationService);

  readonly rangeId = input.required<string>();

  readonly mask = MASK;
  readonly staff = computed(() => roleCan(this.auth.user()?.role, 'range:credentials_read'));
  /** Staff and Students; an observer would only be refused. */
  readonly shown = computed(() => this.staff() || this.auth.user()?.role === 'student');
  readonly title = computed(() => (this.staff() ? 'Credentials' : 'Your accounts'));

  readonly accounts = signal<RangeCredential[] | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  private readonly revealed = signal<ReadonlySet<string>>(new Set());

  constructor() {
    // Another range: forget the previous one's accounts (they were for it only).
    effect(() => {
      this.rangeId();
      untracked(() => this.reset());
    });
  }

  key(a: RangeCredential): string {
    return `${a.machine}\u0000${a.username}`;
  }

  load(): void {
    const id = this.rangeId();
    this.loading.set(true);
    this.error.set('');
    this.api.getCredentials(id).subscribe({
      next: res => {
        if (id !== this.rangeId()) return;
        this.accounts.set(res.accounts);
        this.loading.set(false);
      },
      error: (err: unknown) => {
        if (id !== this.rangeId()) return;
        this.loading.set(false);
        this.error.set(describe(err, this.staff()));
      },
    });
  }

  isRevealed(a: RangeCredential): boolean {
    return this.revealed().has(this.key(a));
  }

  toggle(a: RangeCredential): void {
    const next = new Set(this.revealed());
    const k = this.key(a);
    if (next.has(k)) next.delete(k); else next.add(k);
    this.revealed.set(next);
  }

  copy(text: string, what: 'Username' | 'Password'): void {
    if (this.clipboard.copy(text)) {
      this.notify.success(`${what} copied`);
    } else {
      this.notify.error(`${what} could not be copied`);
    }
  }

  private reset(): void {
    this.accounts.set(null);
    this.error.set('');
    this.loading.set(false);
    this.revealed.set(new Set());
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
