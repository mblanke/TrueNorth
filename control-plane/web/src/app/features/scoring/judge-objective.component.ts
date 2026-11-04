import { Component, computed, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { ApiService, Detection } from '@core/services/api.service';
import { objectiveName } from '@shared/objective-display';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { isDetectionObjective } from '../exercise-detail/detection-panel.component';

/** JudgementIn bounds (app/exercise_judging.py). */
export const MAX_NOTE = 4000;
export const MAX_CITES = 20;
/** Enough of an objective (ObjectiveOut or the scenario-detail row) to judge it. */
export interface JudgeableObjective {
  ref_id: string;
  description?: string | null;
  validator?: string | null;
  achieved: boolean;
}
/** Verdicts an instructor may cite: recorded in this run, or already cited by a withdrawn judgement. */
const CITABLE = ['recorded', 'cited'];

/**
 * An instructor judges one exercise objective (ADR 0020): Acknowledge opens a note and, for a
 * detection objective, the Students' recorded submissions to cite; Withdraw takes a judgement
 * back while the exercise is open. The server decides; the parent reloads on `changed`.
 */
@Component({
  selector: 'tn-judge-objective',
  imports: [DatePipe, FormsModule, MatButtonModule, MatCheckboxModule, MatFormFieldModule, MatIconModule, MatInputModule],
  template: `
    @if (objective().achieved) {
      <button mat-button color="warn" type="button" (click)="withdraw()" [disabled]="busy()">
        <mat-icon>undo</mat-icon> {{ busy() ? 'Withdrawing…' : 'Withdraw' }}
      </button>
    } @else if (!formOpen()) {
      <button mat-button color="primary" type="button" (click)="formOpen.set(true)">
        <mat-icon>check</mat-icon> Acknowledge
      </button>
    } @else {
      <form class="judge" (ngSubmit)="acknowledge()" [attr.aria-label]="'Judge ' + name()">
        <mat-form-field appearance="outline" subscriptSizing="dynamic">
          <mat-label>Note: what the team did</mat-label>
          <textarea matInput name="note" rows="3" [maxlength]="maxNote" [(ngModel)]="note" [disabled]="busy()"></textarea>
        </mat-form-field>
        @if (isDetection()) {
          <fieldset class="cites">
            <legend>Detections to cite <span class="muted">(optional, up to {{ maxCites }})</span></legend>
            @for (d of citable(); track d.id) {
              <mat-checkbox [checked]="cited().has(d.id)" (change)="toggle(d.id)"
                [disabled]="busy() || (!cited().has(d.id) && cited().size >= maxCites)">
                <span class="cite-line">
                  {{ d.submitted_at | date:'shortTime' }} ·
                  <span class="mono" [title]="d.user_id">{{ d.user_id.slice(0, 8) }}</span> ·
                  {{ d.events_matched ?? '—' }} events
                </span>
                <code class="q">{{ d.query }}</code>
              </mat-checkbox>
            } @empty {
              <p class="muted">{{ submissionsError() ? 'The submissions could not be loaded; you can still judge with a note.'
                : 'No detections recorded on this objective in this run.' }}</p>
            }
          </fieldset>
        }
        @if (error(); as e) { <p class="err" role="alert">{{ e }}</p> }
        <div class="acts">
          <button mat-flat-button color="primary" type="submit" [disabled]="busy()">
            {{ busy() ? 'Saving…' : 'Acknowledge' }}
          </button>
          <button mat-button type="button" (click)="cancel()" [disabled]="busy()">Cancel</button>
        </div>
      </form>
    }
    @if (objective().achieved && error(); as e) { <p class="err" role="alert">{{ e }}</p> }
  `,
  styles: [`
    :host { display: block; width: 100%; }
    .judge { display: flex; flex-direction: column; gap: 8px; padding: 4px 8px 8px; }
    .cites { border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; margin: 0; display: flex; flex-direction: column; }
    .cites legend { font-size: 0.82rem; font-weight: 600; padding: 0 4px; }
    .cite-line { font-size: 0.82rem; }
    .mono, .q { font-family: var(--font-mono, monospace); font-size: 12px; }
    .q { display: block; word-break: break-all; color: var(--text-secondary); }
    .muted { color: var(--text-muted); font-size: 0.82rem; font-weight: 400; }
    .err { color: var(--severity-high); font-size: 0.82rem; margin: 0; }
    .acts { display: flex; gap: 8px; }
  `],
})
export class JudgeObjectiveComponent {
  private api = inject(ApiService);
  private dialog = inject(MatDialog);

  readonly exerciseId = input.required<string>();
  readonly objective = input.required<JudgeableObjective>();
  /** This objective's submissions in this run (GET /exercises/{id}/detections, staff view). */
  readonly submissions = input<Detection[]>([]);
  /** The submissions request failed: say so rather than "none recorded". */
  readonly submissionsError = input(false);
  /** Fired after the server accepted a judgement or a withdrawal, or answered that the page is stale. */
  readonly changed = output<void>();

  readonly maxNote = MAX_NOTE;
  readonly maxCites = MAX_CITES;
  readonly formOpen = signal(false);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly cited = signal<ReadonlySet<string>>(new Set());
  note = '';

  readonly name = computed(() => objectiveName(this.objective()));
  readonly isDetection = computed(() => isDetectionObjective(this.objective().validator));
  readonly citable = computed(() => this.submissions().filter(d => CITABLE.includes(d.verdict)));

  toggle(id: string): void {
    const next = new Set(this.cited());
    if (next.has(id)) next.delete(id);
    else if (next.size < MAX_CITES) next.add(id);
    this.cited.set(next);
  }

  cancel(): void {
    this.formOpen.set(false);
    this.error.set(null);
  }

  acknowledge(): void {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    const note = this.note.trim();
    const cites = [...this.cited()];
    this.api.ackObjective(this.exerciseId(), this.objective().ref_id, {
      ...(note ? { evidence: note } : {}),
      ...(cites.length ? { cites } : {}),
    }).subscribe({
      next: () => {
        this.busy.set(false);
        this.formOpen.set(false);
        this.note = '';
        this.cited.set(new Set());
        this.changed.emit();
      },
      error: (err: HttpErrorResponse) => this.fail(err, 'The objective was not acknowledged.'),
    });
  }

  withdraw(): void {
    if (this.busy()) return;
    this.dialog.open(ConfirmDialogComponent, {
      data: {
        title: `Withdraw the judgement on ${this.name()}?`,
        message: 'The objective goes back to not achieved and the score is re-totalled. '
          + 'Detections it cited go back to recorded.',
        confirmText: 'Withdraw',
      },
    }).afterClosed().subscribe(ok => {
      if (!ok) return;
      this.busy.set(true);
      this.error.set(null);
      this.api.withdrawAck(this.exerciseId(), this.objective().ref_id).subscribe({
        next: () => {
          this.busy.set(false);
          this.changed.emit();
        },
        error: (err: HttpErrorResponse) => this.fail(err, 'The judgement was not withdrawn.'),
      });
    });
  }

  private fail(err: HttpErrorResponse, fallback: string): void {
    this.busy.set(false);
    const detail = typeof err?.error?.detail === 'string' ? err.error.detail : '';
    this.error.set(detail ? `${fallback} ${detail}` : fallback);
    // 409: the exercise closed or someone else judged first; the page is stale.
    if (err?.status === 409) this.changed.emit();
  }
}
