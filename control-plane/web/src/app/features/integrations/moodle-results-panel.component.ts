import { Component, OnChanges, inject, input, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatDialog } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { NotificationService } from '@core/services/notification.service';
import {
  MoodleResultsPull, MoodleResultsStatus, OperatorRecordsApiService,
} from '@core/services/operator-records-api.service';
import {
  ImpactDialogComponent, ImpactDialogData, ImpactDialogResult,
} from '../../shared/components/impact-dialog/impact-dialog.component';

function apiError(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (typeof err.error?.detail === 'string') return err.error.detail;
    if (err.status === 403) return 'Your role does not allow this.';
    if (err.status === 0) return 'The API could not be reached.';
    return `The API answered ${err.status}.`;
  }
  return 'The request failed.';
}

/**
 * Where TrueNorth is in one Moodle's results (completions and quiz grades pulled back,
 * app/moodle_results), and "Pull now" (R0-B). Stale is the API's judgement, shown with
 * its reason; a failed pull shows the Moodle's own error.
 */
@Component({
  selector: 'tn-moodle-results-panel',
  imports: [CommonModule, FormsModule, MatButtonModule, MatCheckboxModule, MatIconModule],
  template: `
    <section class="results" [attr.aria-label]="'Moodle results for ' + platformName()">
      <h4>Results from Moodle</h4>
      @if (error()) {
        <p class="error-text" role="alert">{{ error() }}</p>
      } @else if (!status()) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
      } @else {
        @let s = status()!;
        <p class="state" role="status">
          @if (s.running) {
            <span class="badge running">Pull running</span>
          } @else if (s.stale) {
            <span class="badge stale">Stale</span> {{ s.stale_reason }}
          } @else {
            <span class="badge fresh">Up to date</span> {{ s.stale_reason }}
          }
        </p>
        <dl class="facts">
          <dt>Last successful pull</dt><dd>{{ s.last_success_at ? (s.last_success_at | date:'medium') : 'never' }}</dd>
          <dt>Last attempt</dt><dd>{{ s.last_run_at ? (s.last_run_at | date:'medium') : 'never' }}</dd>
          <dt>Schedule</dt><dd>{{ s.scheduled_every_seconds ? 'every ' + (s.scheduled_every_seconds / 60 | number:'1.0-1') + ' min' : 'off (pull by hand)' }}</dd>
          <dt>Rows read / recorded</dt><dd>{{ s.rows_seen ?? 0 }} / {{ s.rows_applied ?? 0 }}</dd>
          @if (s.last_error) { <dt>Last error</dt><dd class="error-text">{{ s.last_error }}</dd> }
        </dl>
      }

      @if (canPull()) {
        <div class="pull">
          <mat-checkbox [(ngModel)]="reset" name="reset">Read from the beginning</mat-checkbox>
          <button mat-stroked-button color="primary" (click)="pull()" [disabled]="busy() || !!status()?.running">
            <mat-icon>sync</mat-icon> {{ busy() ? 'Pulling…' : 'Pull now' }}
          </button>
        </div>
      }
      @if (outcome()) { <p class="outcome" role="status" aria-live="polite">{{ outcome() }}</p> }
    </section>
  `,
  styles: [`
    .results { border-top: 1px solid var(--border); margin-top: 12px; padding-top: 8px; }
    h4 { margin: 0 0 6px; font-size: 14px; }
    .facts { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; font-size: 12px; }
    .facts dt { color: var(--text-secondary); }
    .state { font-size: 12px; margin: 0 0 6px; }
    .badge { padding: 1px 8px; border-radius: 10px; font-weight: 600; margin-right: 6px; }
    .badge.fresh { background: var(--success, #22c55e); color: #fff; }
    .badge.stale { background: var(--warning, #f59e0b); color: #000; }
    .badge.running { background: var(--accent); color: #fff; }
    .pull { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
    .error-text { color: var(--alert, #ef4444); }
    .outcome { border-left: 4px solid var(--accent); padding: 4px 8px; font-size: 12px; background: var(--bg-surface); }
  `],
})
export class MoodleResultsPanelComponent implements OnChanges {
  private api = inject(OperatorRecordsApiService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);

  readonly platformId = input.required<string>();
  readonly platformName = input('');
  /** integration:write (admins); the API checks it again. */
  readonly canPull = input(false);

  status = signal<MoodleResultsStatus | null>(null);
  error = signal('');
  busy = signal(false);
  outcome = signal('');
  reset = false;

  ngOnChanges(): void {
    this.load();
  }

  load(): void {
    this.api.getMoodleResults(this.platformId()).subscribe({
      next: s => { this.status.set(s); this.error.set(''); },
      error: err => this.error.set(apiError(err)),
    });
  }

  pull(): void {
    const reset = this.reset;
    const data: ImpactDialogData = {
      title: `Pull results from ${this.platformName() || 'this Moodle'} now?`,
      impact: [
        reset
          ? 'Reads every completion and quiz grade from the beginning again. Recording is idempotent: rows already recorded are only re-checked.'
          : 'Reads the completions and quiz grades recorded since the last pull.',
        "They are recorded on the Students' course records in TrueNorth; a pass is never undone by a lower grade.",
        'The answer must be signed by the Moodle\'s registered key, or nothing is recorded. The pull is audited.',
      ],
      confirmText: 'Pull now',
    };
    this.dialog
      .open<ImpactDialogComponent, ImpactDialogData, ImpactDialogResult | null>(ImpactDialogComponent, {
        width: '520px', data,
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.busy.set(true);
        this.outcome.set('');
        this.api.pullMoodleResults(this.platformId(), reset).subscribe({
          next: out => {
            this.busy.set(false);
            this.outcome.set(describePull(out));
            this.load();
          },
          error: err => {
            this.busy.set(false);
            const msg = apiError(err);
            this.outcome.set(`Pull failed: ${msg}`);
            this.notify.error(msg);
            this.load();
          },
        });
      });
  }
}

export function describePull(out: MoodleResultsPull): string {
  const skipped = Object.entries(out.skipped || {}).map(([k, n]) => `${n} ${k}`).join(', ');
  return `Read ${out.rows} row(s) in ${out.pages} page(s): ${out.applied} recorded, ${out.unchanged} unchanged`
    + (skipped ? `, skipped ${skipped}` : '')
    + (out.more ? '. More remain; the next pull continues.' : '.');
}
