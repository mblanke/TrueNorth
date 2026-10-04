import { Component, OnInit, signal, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { ExerciseSummary, RangeSummary, ScenarioSummary } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

@Component({
  selector: 'tn-exercises',
  imports: [
    CommonModule, FormsModule, RouterLink, MatCardModule, MatTableModule,
    MatButtonModule, MatIconModule, MatFormFieldModule, MatInputModule,
    MatSelectModule, MatChipsModule, MatTooltipModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">fitness_center</mat-icon>
          <div>
            <div class="tn-kicker">Prepare / Run / Review</div>
            <h1>Exercises</h1>
            <p class="subtitle">Bring the scenario, range, and training evidence together.</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="showCreate = !showCreate">
          <mat-icon>add</mat-icon> New Exercise
        </button>
      </div>

      @if (showCreate) {
        <mat-card class="mt-2">
          <mat-card-content>
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Name</mat-label>
              <input matInput [(ngModel)]="form.name" placeholder="IR Drill #1">
            </mat-form-field>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Range</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="form.range_id">
                @for (r of ranges(); track r.id) {
                  <mat-option [value]="r.id">{{ r.name }} ({{ r.state }})</mat-option>
                }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Scenario</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="form.scenario_id">
                @for (s of scenarios(); track s.id) {
                  <mat-option [value]="s.id">{{ s.name }}</mat-option>
                }
              </mat-select>
                </mat-form-field>
              </div>
              <div>
                <button mat-raised-button color="primary" (click)="create()" [disabled]="!form.name || !form.range_id || !form.scenario_id">Create</button>
              <button mat-button (click)="showCreate = false">Cancel</button>
            </div>
          </mat-card-content>
        </mat-card>
      }

      @if (editingId) {
        <mat-card class="edit-form mt-2">
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Exercise Name</mat-label>
                <input matInput [(ngModel)]="editForm.name" placeholder="Exercise name">
              </mat-form-field>
            </div>
            <button mat-raised-button color="primary" (click)="updateExercise()" [disabled]="!editForm.name || saving">
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
            <button mat-button (click)="cancelEdit()" [disabled]="saving">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      <section class="exercise-workspace">
      <header class="list-heading"><h2>Training runs</h2><span>Open a run to prepare, operate, or review it.</span></header>
      <div class="table-wrap">
      <table mat-table [dataSource]="exercises()" class="full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let e"><a [routerLink]="['/exercises', e.id]">{{ e.name }}</a></td>
        </ng-container>
        <ng-container matColumnDef="state">
          <th mat-header-cell *matHeaderCellDef>State</th>
          <td mat-cell *matCellDef="let e">
            <span class="status-chip" [class]="e.state">{{ e.state }}</span>
          </td>
        </ng-container>
        <ng-container matColumnDef="score">
          <th mat-header-cell *matHeaderCellDef>Score</th>
          <td mat-cell *matCellDef="let e">
            <span class="score-cell">
              <span>{{ e.total_score }}/{{ e.max_score }}</span>
              <span class="tn-gauge-bg score-gauge" aria-hidden="true">
                <span class="tn-gauge-fill" [style.width.%]="e.max_score ? (e.total_score / e.max_score) * 100 : 0"></span>
              </span>
            </span>
          </td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let e">{{ e.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let e">
            <a mat-button [routerLink]="['/exercises', e.id]">Open</a>
            @if (e.state === 'pending') {
              <button mat-icon-button (click)="startEdit(e)" matTooltip="Rename" aria-label="Rename exercise" [disabled]="saving">
                <mat-icon>edit</mat-icon>
              </button>
              <button mat-icon-button color="primary" (click)="start(e.id)" matTooltip="Start" aria-label="Start exercise">
                <mat-icon>play_arrow</mat-icon>
              </button>
            }
            @if (e.state === 'running') {
              <button mat-icon-button (click)="pause(e.id)" matTooltip="Pause" aria-label="Pause exercise">
                <mat-icon>pause</mat-icon>
              </button>
              <button mat-icon-button color="accent" (click)="complete(e.id)" matTooltip="Complete" aria-label="Complete exercise">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (e.state === 'paused') {
              <button mat-icon-button color="primary" (click)="start(e.id)" matTooltip="Resume" aria-label="Resume exercise">
                <mat-icon>play_arrow</mat-icon>
              </button>
              <button mat-icon-button color="accent" (click)="complete(e.id)" matTooltip="Complete" aria-label="Complete exercise">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (e.state === 'completed') {
              <button mat-icon-button (click)="genAAR(e.id)" matTooltip="Generate AAR" aria-label="Generate after-action report">
                <mat-icon>assessment</mat-icon>
              </button>
              <button mat-icon-button (click)="downloadAARPdf(e.id)" matTooltip="Download AAR PDF" aria-label="Download after-action report PDF">
                <mat-icon>picture_as_pdf</mat-icon>
              </button>
            }
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="columns"></tr>
        <tr mat-row *matRowDef="let row; columns: columns"></tr>
      </table>
      </div>

      @if (loading()) {
        <div class="tn-skeleton-group mt-2" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
        </div>
      } @else if (loadError()) {
        <tn-empty-state icon="error_outline" title="Exercises could not be loaded" message="Try again to retrieve the training runs.">
          <button mat-stroked-button (click)="load()">Retry</button>
        </tn-empty-state>
      } @else if (exercises().length === 0) {
        <tn-empty-state
          icon="fitness_center"
          title="No exercises yet"
          message="Create one from a range and a scenario to start a training run."
        >
          <button mat-stroked-button (click)="showCreate = true">
            <mat-icon>add</mat-icon> New Exercise
          </button>
        </tn-empty-state>
      }
      </section>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .exercise-workspace { padding: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); }
    .list-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
    .list-heading h2 { font-size: 17px; font-weight: 600; }
    .list-heading span { font-size: 13px; color: var(--text-muted); }
    td a:not([mat-button]) { color: var(--text-primary); font-weight: 600; text-decoration: none; }
    td a:not([mat-button]):hover { color: var(--accent); text-decoration: underline; }
    .full-width { width: 100%; }
    mat-card-content { display: flex; flex-direction: column; gap: 12px; }
    mat-card-content mat-form-field:not(.full-width) { width: 100%; max-width: 400px; }
    .edit-form mat-card-content { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; flex-direction: row; }
    .edit-form mat-form-field { flex: 1; min-width: 200px; }
    .score-cell { display: flex; align-items: center; gap: 8px; }
    .score-gauge { display: inline-block; width: 64px; height: 6px; }
    .score-gauge .tn-gauge-fill { display: block; }
  `],
})
export class ExercisesComponent implements OnInit {
  private api = inject(ApiService);
  private notify = inject(NotificationService);

  exercises = signal<ExerciseSummary[]>([]);
  ranges = signal<RangeSummary[]>([]);
  scenarios = signal<ScenarioSummary[]>([]);
  loading = signal(true);
  loadError = signal(false);
  showCreate = false;
  form = { name: '', range_id: '', scenario_id: '' };
  columns = ['name', 'state', 'score', 'created', 'actions'];
  editingId: string | null = null;
  editForm = { name: '' };
  saving = false;

  ngOnInit(): void {
    this.load();
    this.api.listRanges().subscribe(r => this.ranges.set(r));
    this.api.listScenarios().subscribe(s => this.scenarios.set(s));
  }

  load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.api.listExercises().subscribe({
      next: e => { this.exercises.set(e); this.loading.set(false); },
      error: () => { this.loading.set(false); this.loadError.set(true); },
    });
  }

  create(): void {
    this.api.createExercise(this.form).subscribe({
      next: () => { this.notify.success('Exercise created'); this.load(); this.showCreate = false; },
      error: () => this.notify.error('Failed to create exercise'),
    });
  }

  start(id: string): void {
    this.api.startExercise(id).subscribe({
      next: () => { this.notify.success('Exercise started'); this.load(); },
      error: (err) => this.notify.error(err?.error?.detail || 'Start failed'),
    });
  }

  pause(id: string): void {
    this.api.pauseExercise(id).subscribe({
      next: () => { this.notify.success('Exercise paused'); this.load(); },
      error: () => this.notify.error('Pause failed'),
    });
  }

  complete(id: string): void {
    this.api.completeExercise(id).subscribe({
      next: () => { this.notify.success('Exercise completed'); this.load(); },
      error: () => this.notify.error('Complete failed'),
    });
  }

  genAAR(id: string): void {
    this.api.generateAAR(id).subscribe({
      next: () => this.notify.success('AAR generated'),
      error: () => this.notify.error('AAR generation failed'),
    });
  }

  downloadAARPdf(id: string): void {
    this.api.getAARPdf(id).subscribe({
      next: (blob) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `aar-${id}.pdf`;
        a.click();
        window.URL.revokeObjectURL(url);
        this.notify.success('AAR PDF downloaded');
      },
      error: (err) => this.notify.error(err?.error?.detail || 'PDF download failed. Generate AAR first.'),
    });
  }

  startEdit(e: ExerciseSummary): void {
    this.editingId = e.id;
    this.editForm.name = e.name;
  }

  updateExercise(): void {
    this.saving = true;
    this.api.updateExercise(this.editingId!, this.editForm).subscribe({
      next: () => { this.notify.success('Exercise renamed'); this.load(); this.cancelEdit(); this.saving = false; },
      error: () => { this.notify.error('Failed to rename exercise'); this.saving = false; },
    });
  }

  cancelEdit(): void {
    this.editingId = null;
    this.editForm.name = '';
  }
}
