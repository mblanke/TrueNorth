import { Component, Inject, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MAT_DIALOG_DATA, MatDialog, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService, CollectiveExercise } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Range as RangeModel } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { CountUpDirective, EnterStaggerDirective, HoverLiftDirective } from '../../shared/motion';

interface ObjectiveDraft {
  ref: string;
  text: string;
  moe: string;
}

/** Create a collective exercise, with its first objectives inline. */
@Component({
  selector: 'tn-collective-create-dialog',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule,
    MatIconModule, MatInputModule, MatSelectModule,
  ],
  template: `
    <h2 mat-dialog-title>New collective exercise</h2>
    <mat-dialog-content>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Name</mat-label>
        <input matInput name="name" [(ngModel)]="name" placeholder="COALITION SHIELD 26" />
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Range</mat-label>
        <mat-select name="range" [(ngModel)]="rangeId" panelClass="tn-select-panel">
          @for (r of data.ranges; track r.id) {
            <mat-option [value]="r.id">{{ r.name }} ({{ r.state }})</mat-option>
          }
        </mat-select>
      </mat-form-field>

      <p class="hint">Objectives can be added now or imported from a planner CSV later.</p>
      @for (o of objectives; track i; let i = $index) {
        <div class="obj-row">
          <mat-form-field appearance="outline" class="ref">
            <mat-label>Ref</mat-label>
            <input matInput [name]="'ref' + i" [(ngModel)]="o.ref" placeholder="O1" />
          </mat-form-field>
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Objective</mat-label>
            <input matInput [name]="'text' + i" [(ngModel)]="o.text" />
          </mat-form-field>
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Measure of effectiveness</mat-label>
            <input matInput [name]="'moe' + i" [(ngModel)]="o.moe" />
          </mat-form-field>
          <button mat-icon-button color="warn" (click)="objectives.splice(i, 1)" matTooltip="Remove">
            <mat-icon>close</mat-icon>
          </button>
        </div>
      }
      <button mat-stroked-button (click)="addObjective()">
        <mat-icon>add</mat-icon> Add objective
      </button>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="ref.close()">Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!name.trim() || !rangeId" (click)="submit()">
        Create
      </button>
    </mat-dialog-actions>
  `,
  styles: [`
    .w { width: 100%; display: block; }
    .hint { color: var(--text-secondary); font-size: 0.86rem; margin: 4px 0 10px; }
    .obj-row { display: flex; gap: 8px; align-items: flex-start; }
    .obj-row .ref { width: 90px; }
    .obj-row .grow { flex: 1; min-width: 140px; }
  `],
})
export class CollectiveCreateDialogComponent {
  name = '';
  rangeId = '';
  objectives: ObjectiveDraft[] = [];

  constructor(
    public ref: MatDialogRef<CollectiveCreateDialogComponent>,
    @Inject(MAT_DIALOG_DATA) public data: { ranges: RangeModel[] },
  ) {}

  addObjective(): void {
    this.objectives.push({ ref: `O${this.objectives.length + 1}`, text: '', moe: '' });
  }

  submit(): void {
    this.ref.close({
      name: this.name.trim(),
      range_id: this.rangeId,
      objectives: this.objectives.filter(o => o.text.trim()),
    });
  }
}

/**
 * The MESL tab's front door: collective exercises and their Master Event
 * Sequence Lists. The whole /collective-exercises capability — structured
 * serials, a planner-CSV importer, AI drafting — previously had no UI at all.
 */
@Component({
  selector: 'tn-mesl-home',
  standalone: true,
  imports: [
    CommonModule, RouterLink, MatButtonModule, MatCardModule, MatDialogModule,
    MatIconModule, MatTooltipModule,
    EmptyStateComponent, CountUpDirective, EnterStaggerDirective, HoverLiftDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">event_note</mat-icon>
          <div>
            <h1>Collective Exercises</h1>
            <p class="subtitle">Master Event Sequence Lists — objectives, serials, and delivery</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="create()">
          <mat-icon>add</mat-icon> New Exercise
        </button>
      </div>

      @if (loading()) {
        <div class="tn-skeleton-group mt-2" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-card"></div>
          <div class="tn-skeleton tn-skeleton-card"></div>
        </div>
      } @else if (!exercises().length) {
        <tn-empty-state icon="event_note" title="No collective exercises yet"
                        message="Create one, then import a planner MESL or draft one with AI.">
          <button mat-stroked-button (click)="create()">
            <mat-icon>add</mat-icon> New Exercise
          </button>
        </tn-empty-state>
      } @else {
        <div class="grid" tnEnterStagger>
          @for (ex of exercises(); track ex.id) {
            <mat-card class="ex-card tn-stagger-item" tnHoverLift>
              <mat-card-content>
                <div class="ex-head">
                  <span class="ex-name">{{ ex.name }}</span>
                  <span class="status-chip" [class]="ex.state">{{ ex.state }}</span>
                </div>
                <div class="ex-stats">
                  <span class="stat">
                    <span class="stat-value" [tnCountUp]="ex.objectives"></span>
                    <span class="stat-label">objectives</span>
                  </span>
                  <span class="stat">
                    <span class="stat-value" [tnCountUp]="ex.mesl_events"></span>
                    <span class="stat-label">serials</span>
                  </span>
                </div>
              </mat-card-content>
              <mat-card-actions align="end">
                <a mat-stroked-button [routerLink]="['/authoring/mesl', ex.id]">
                  <mat-icon>list_alt</mat-icon> Open MESL
                </a>
              </mat-card-actions>
            </mat-card>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; margin-top: 16px; }
    .ex-head { display: flex; align-items: center; gap: 10px; justify-content: space-between; }
    .ex-name { font-weight: 600; color: var(--text-primary); }
    .ex-stats { display: flex; gap: 24px; margin-top: 12px; }
    .stat { display: flex; flex-direction: column; }
    .stat-value { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--text-primary); }
    .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); }
  `],
})
export class MeslHomeComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);

  protected readonly exercises = signal<CollectiveExercise[]>([]);
  protected readonly ranges = signal<RangeModel[]>([]);
  protected readonly loading = signal(true);

  ngOnInit(): void {
    this.load();
    this.api.listRanges().subscribe({ next: r => this.ranges.set(r), error: () => this.ranges.set([]) });
  }

  private load(): void {
    this.api.listCollectiveExercises().subscribe({
      next: x => { this.exercises.set(x); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }

  protected create(): void {
    this.dialog
      .open(CollectiveCreateDialogComponent, { data: { ranges: this.ranges() }, width: '720px' })
      .afterClosed()
      .subscribe(body => {
        if (!body) return;
        this.api.createCollectiveExercise(body).subscribe({
          next: () => { this.notify.success('Exercise created'); this.load(); },
          error: () => this.notify.error('Could not create the exercise'),
        });
      });
  }
}
