import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
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
import { Exercise, Range, Scenario } from '@core/models';

@Component({
  selector: 'tn-exercises',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatTableModule,
    MatButtonModule, MatIconModule, MatFormFieldModule, MatInputModule,
    MatSelectModule, MatChipsModule, MatTooltipModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">fitness_center</mat-icon>
          <div>
            <h1>Exercises</h1>
            <p class="subtitle">Create and manage training exercises</p>
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

      <table mat-table [dataSource]="exercises()" class="mt-2 full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let e">{{ e.name }}</td>
        </ng-container>
        <ng-container matColumnDef="state">
          <th mat-header-cell *matHeaderCellDef>State</th>
          <td mat-cell *matCellDef="let e">
            <span class="status-chip" [class]="e.state">{{ e.state }}</span>
          </td>
        </ng-container>
        <ng-container matColumnDef="score">
          <th mat-header-cell *matHeaderCellDef>Score</th>
          <td mat-cell *matCellDef="let e">{{ e.total_score }}/{{ e.max_score }}</td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let e">{{ e.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let e">
            @if (e.state === 'pending') {
              <button mat-icon-button (click)="startEdit(e)" matTooltip="Rename" [disabled]="saving">
                <mat-icon>edit</mat-icon>
              </button>
              <button mat-icon-button color="primary" (click)="start(e.id)" matTooltip="Start">
                <mat-icon>play_arrow</mat-icon>
              </button>
            }
            @if (e.state === 'running') {
              <button mat-icon-button (click)="pause(e.id)" matTooltip="Pause">
                <mat-icon>pause</mat-icon>
              </button>
              <button mat-icon-button color="accent" (click)="complete(e.id)" matTooltip="Complete">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (e.state === 'paused') {
              <button mat-icon-button color="primary" (click)="start(e.id)" matTooltip="Resume">
                <mat-icon>play_arrow</mat-icon>
              </button>
              <button mat-icon-button color="accent" (click)="complete(e.id)" matTooltip="Complete">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (e.state === 'completed') {
              <button mat-icon-button (click)="genAAR(e.id)" matTooltip="Generate AAR">
                <mat-icon>assessment</mat-icon>
              </button>
              <button mat-icon-button (click)="downloadAARPdf(e.id)" matTooltip="Download AAR PDF">
                <mat-icon>picture_as_pdf</mat-icon>
              </button>
            }
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="columns"></tr>
        <tr mat-row *matRowDef="let row; columns: columns"></tr>
      </table>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .full-width { width: 100%; }
    mat-card-content { display: flex; flex-direction: column; gap: 12px; }
    mat-card-content mat-form-field:not(.full-width) { width: 100%; max-width: 400px; }
    .edit-form mat-card-content { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; flex-direction: row; }
    .edit-form mat-form-field { flex: 1; min-width: 200px; }
  `],
})
export class ExercisesComponent implements OnInit {
  exercises = signal<Exercise[]>([]);
  ranges = signal<Range[]>([]);
  scenarios = signal<Scenario[]>([]);
  showCreate = false;
  form = { name: '', range_id: '', scenario_id: '' };
  columns = ['name', 'state', 'score', 'created', 'actions'];
  editingId: string | null = null;
  editForm = { name: '' };
  saving = false;

  constructor(private api: ApiService, private notify: NotificationService) {}

  ngOnInit(): void {
    this.load();
    this.api.listRanges().subscribe(r => this.ranges.set(r));
    this.api.listScenarios().subscribe(s => this.scenarios.set(s));
  }

  load(): void { this.api.listExercises().subscribe(e => this.exercises.set(e)); }

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

  startEdit(e: Exercise): void {
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
