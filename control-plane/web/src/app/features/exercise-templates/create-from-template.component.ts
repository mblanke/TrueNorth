import { ChangeDetectionStrategy, Component, OnInit, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { ApiService } from '@core/services/api.service';
import { ExerciseTemplatesApiService } from '@core/services/exercise-templates-api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeSummary } from '@core/models';
import { apiErrorMessage } from '../../shared/kb-errors';

/**
 * "Create exercise from template" (ADR 0017): a name and the range it runs on. The API copies
 * the template's objectives and MESL into the new exercise and refuses a range that was not
 * built from the template's range template, with the reason, which is shown here.
 */
@Component({
  selector: 'tn-create-from-template',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatButtonModule, MatFormFieldModule, MatInputModule, MatSelectModule],
  template: `
    <form class="create" (ngSubmit)="create()" aria-label="Create exercise from template">
      <mat-form-field appearance="outline">
        <mat-label>Exercise name</mat-label>
        <input matInput name="name" [(ngModel)]="name" required placeholder="IR drill, week 3">
      </mat-form-field>
      <mat-form-field appearance="outline">
        <mat-label>Range</mat-label>
        <mat-select name="range" panelClass="tn-select-panel" [(ngModel)]="rangeId" required>
          @for (r of ranges(); track r.id) {
            <mat-option [value]="r.id">{{ r.name }} ({{ r.state }})</mat-option>
          }
        </mat-select>
        <mat-hint>Built from the range template this exercise template runs on.</mat-hint>
      </mat-form-field>
      @if (error()) { <p class="err" role="alert">{{ error() }}</p> }
      <div class="actions">
        <button mat-raised-button color="primary" type="submit" [disabled]="!name.trim() || !rangeId || busy()">
          {{ busy() ? 'Creating...' : 'Create exercise' }}
        </button>
        <button mat-button type="button" (click)="cancelled.emit()" [disabled]="busy()">Cancel</button>
      </div>
    </form>
  `,
  styles: [`
    .create { display: flex; flex-direction: column; gap: 12px; max-width: 420px; }
    .actions { display: flex; gap: 8px; }
    .err { margin: 0; color: var(--mat-sys-error); font-size: 13px; }
  `],
})
export class CreateFromTemplateComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly templates = inject(ExerciseTemplatesApiService);
  private readonly notify = inject(NotificationService);
  private readonly router = inject(Router);

  readonly templateId = input.required<string>();
  readonly templateName = input<string>('');
  readonly cancelled = output<void>();
  readonly created = output<string>();

  readonly ranges = signal<RangeSummary[]>([]);
  readonly busy = signal(false);
  readonly error = signal('');
  name = '';
  rangeId = '';

  ngOnInit(): void {
    this.name = this.templateName();
    this.api.listRanges(200).subscribe({
      next: r => this.ranges.set(r),
      error: () => this.error.set('Ranges could not be loaded.'),
    });
  }

  create(): void {
    if (this.busy() || !this.name.trim() || !this.rangeId) return;
    this.busy.set(true);
    this.error.set('');
    this.templates.createExercise(this.templateId(), { name: this.name.trim(), range_id: this.rangeId }).subscribe({
      next: ex => {
        this.busy.set(false);
        this.notify.success('Exercise created from the template');
        this.created.emit(ex.id);
        void this.router.navigate(['/exercises', ex.id]);
      },
      error: err => {
        this.busy.set(false);
        this.error.set(apiErrorMessage(err, 'The exercise could not be created.'));
      },
    });
  }
}
