import { Component, computed, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { ApiService, Detection } from '@core/services/api.service';

/** `validate.opensearch.query` -> `opensearch_query` (mirrors app/detections/names.py). */
export function canonicalValidator(name: string | null | undefined): string {
  let raw = (name ?? '').trim().toLowerCase();
  for (const prefix of ['validate.', 'validate_']) {
    if (raw.startsWith(prefix)) { raw = raw.slice(prefix.length); break; }
  }
  return raw.replace(/\./g, '_');
}

/** Detection objectives are worked by a submitted query; an instructor judges them (ADR 0020). */
export function isDetectionObjective(validator: string | null | undefined): boolean {
  return canonicalValidator(validator) === 'opensearch_query';
}

type Tone = 'ok' | 'warn' | 'err';

/** Below this many submissions left, the panel says how many remain. */
export const LOW_SUBMISSIONS = 5;

/** How a submission's stored verdict reads. Older rows keep their ADR 0005 verdicts as history. */
export function verdictWord(verdict: string, staff: boolean): string {
  if (verdict === 'recorded') return 'Recorded';
  if (verdict === 'cited') return staff ? 'Cited' : 'Cited by your instructor';
  const v = (verdict ?? '').trim();
  return v ? v.charAt(0).toUpperCase() + v.slice(1) : '—';
}

/**
 * Submit a detection for one objective and list the submissions on it.
 * A submission is evidence, not a grade (ADR 0020): the server records it and an
 * instructor judges the objective. The parent reloads the objective, score and
 * submissions when `changed` fires.
 */
@Component({
  selector: 'tn-detection-panel',
  imports: [DatePipe, FormsModule, MatButtonModule, MatFormFieldModule, MatIconModule, MatInputModule],
  template: `
    <div class="det-actions">
      @if (canSubmitNow()) {
        <button mat-button type="button" (click)="open.set(!open())" [attr.aria-expanded]="open()"
          [disabled]="submissionsLeft() === 0">
          <mat-icon>manage_search</mat-icon> Submit detection
        </button>
      }
      @if (attempts().length) {
        <button mat-button type="button" class="attempts-toggle" (click)="showAttempts.set(!showAttempts())"
          [attr.aria-expanded]="showAttempts()">
          <mat-icon>history</mat-icon> Submissions ({{ attempts().length }})
        </button>
      }
    </div>

    @if (open() && canSubmitNow()) {
      <form class="det-form" (ngSubmit)="submit()">
        <mat-form-field appearance="outline" subscriptSizing="dynamic" class="det-field">
          <mat-label>Detection query</mat-label>
          <textarea matInput name="query" rows="3" maxlength="2000" required [(ngModel)]="query"
            [disabled]="busy()" placeholder='process.name:"powershell.exe" AND ...'></textarea>
          <mat-hint>field:value terms with AND / OR / NOT, * wildcards, field:&gt;N, field:(a OR b)</mat-hint>
        </mat-form-field>
        <button mat-flat-button color="primary" type="submit"
          [disabled]="busy() || !query.trim() || submissionsLeft() === 0">
          {{ busy() ? 'Recording…' : 'Submit' }}
        </button>
      </form>
    }

    @if (message(); as m) {
      <p class="det-msg" [class]="m.tone" role="status" aria-live="polite">{{ m.text }}</p>
    }

    @if (showAttempts() && attempts().length) {
      <table class="det-table">
        <thead>
          <tr>
            <th scope="col">Time</th>
            @if (staff()) { <th scope="col">Student</th> }
            <th scope="col">Query</th>
            <th scope="col">Status</th>
            <th scope="col">Events</th>
          </tr>
        </thead>
        <tbody>
          @for (a of attempts(); track a.id) {
            <tr>
              <td>{{ a.submitted_at | date:'shortTime' }}</td>
              @if (staff()) { <td class="mono" [title]="a.user_id">{{ a.user_id.slice(0, 8) }}</td> }
              <td class="mono q">{{ a.query }}</td>
              <td [class]="'v ' + a.verdict">{{ word(a.verdict) }}</td>
              <td>{{ a.events_matched ?? '—' }}</td>
            </tr>
          }
        </tbody>
      </table>
    }
  `,
  styles: [`
    :host { display: block; padding: 0 0 6px 32px; }
    .det-actions { display: flex; gap: 4px; flex-wrap: wrap; }
    .det-form { display: flex; gap: 8px; align-items: flex-start; margin: 6px 0; }
    .det-field { flex: 1 1 auto; }
    .det-field textarea, .mono { font-family: var(--font-mono, monospace); font-size: 12px; }
    .det-msg { margin: 4px 0; font-size: 0.82rem; }
    .det-msg.ok { color: var(--success); }
    .det-msg.warn { color: var(--warning); }
    .det-msg.err { color: var(--severity-high); }
    .det-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 4px; }
    .det-table th, .det-table td { text-align: left; padding: 3px 6px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .det-table th { color: var(--text-muted); font-weight: 600; }
    .det-table .q { word-break: break-all; }
    .v.cited { color: var(--success); }
    .v.recorded { color: var(--text-secondary); }
  `],
})
export class DetectionPanelComponent {
  private api = inject(ApiService);

  readonly exerciseId = input.required<string>();
  readonly refId = input.required<string>();
  readonly achieved = input(false);
  readonly running = input(false);
  /** Role may submit (detection:submit). */
  readonly canSubmit = input(false);
  /** Instructor/admin: the list holds every Student's submissions, so it names the Student. */
  readonly staff = input(false);
  readonly attempts = input<Detection[]>([]);
  /** Fired after any outcome that may change the objective, score or attempts. */
  readonly changed = output<void>();

  readonly open = signal(false);
  readonly showAttempts = signal(false);
  readonly busy = signal(false);
  readonly submissionsLeft = signal<number | null>(null);
  readonly message = signal<{ text: string; tone: Tone } | null>(null);
  query = '';

  word(verdict: string): string { return verdictWord(verdict, this.staff()); }

  readonly canSubmitNow = computed(() => this.canSubmit() && this.running() && !this.achieved());

  submit(): void {
    const query = this.query.trim();
    if (!query || this.busy()) return;
    this.busy.set(true);
    this.message.set(null);
    this.api.submitDetection(this.exerciseId(), this.refId(), query).subscribe({
      next: d => {
        this.busy.set(false);
        const left = d.submissions_left ?? null;
        this.submissionsLeft.set(left);
        const tail = left != null && left <= LOW_SUBMISSIONS
          ? ` ${left} submission${left === 1 ? '' : 's'} left.` : '';
        const n = d.events_matched ?? 0;
        this.message.set({
          text: `Recorded: your query matched ${n} event${n === 1 ? '' : 's'}. Your instructor judges the objective.${tail}`,
          tone: 'ok',
        });
        this.changed.emit();
      },
      error: (err: HttpErrorResponse) => {
        this.busy.set(false);
        const detail = typeof err.error?.detail === 'string' ? err.error.detail : '';
        if (err.status === 503) {
          this.message.set({ text: 'The event store could not run this query. Try again; nothing was recorded.', tone: 'err' });
          return;
        }
        if (err.status === 422) {
          // Outside the detection language or unparseable: the server says why.
          const why = detail || 'The query could not be read.';
          this.message.set({ text: /nothing (was )?recorded/i.test(why) ? why : `${why} Nothing was recorded.`, tone: 'warn' });
          return;
        }
        if (err.status === 429) {
          this.submissionsLeft.set(0);
          this.message.set({ text: detail || 'You have reached the submission limit for this objective in this run.', tone: 'err' });
          return;
        }
        if (err.status === 409) {
          // Exercise no longer running, or objective already achieved: the page is stale.
          this.message.set({ text: detail || 'This objective is not accepting detections.', tone: 'err' });
          this.changed.emit();
          return;
        }
        this.message.set({ text: detail || 'Could not submit the detection.', tone: 'err' });
      },
    });
  }
}
