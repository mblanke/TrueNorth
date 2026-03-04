import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatTableModule } from '@angular/material/table';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Exercise, Objective, AAR } from '@core/models';

@Component({
  selector: 'tn-scoring',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatSelectModule, MatFormFieldModule, MatInputModule, MatTableModule,
    MatProgressBarModule, MatCheckboxModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">scoreboard</mat-icon>
          <div>
            <h1>Scoring & After Action Review</h1>
            <p class="subtitle">Exercise scoring, objective tracking, and post-exercise analysis</p>
          </div>
        </div>
      </div>

      <mat-form-field appearance="outline">
        <mat-label>Select Exercise</mat-label>
        <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedExerciseId" (selectionChange)="loadExercise()">
          @for (ex of exercises(); track ex.id) {
            <mat-option [value]="ex.id">{{ ex.name }} ({{ ex.state }})</mat-option>
          }
        </mat-select>
      </mat-form-field>

      @if (selectedExercise()) {
        <mat-card class="mt-2">
          <mat-card-header>
            <mat-card-title>{{ selectedExercise()!.name }}</mat-card-title>
            <mat-card-subtitle>
              State: {{ selectedExercise()!.state }} |
              Score: {{ selectedExercise()!.total_score }}/{{ selectedExercise()!.max_score }}
              ({{ scorePercent() }}%)
            </mat-card-subtitle>
          </mat-card-header>
          <mat-card-content>
            <mat-progress-bar mode="determinate" [value]="scorePercent()"></mat-progress-bar>
          </mat-card-content>
        </mat-card>

        <h2 class="mt-3">Objectives</h2>
        <div class="card-grid">
          @for (obj of objectives(); track obj.id) {
            <mat-card [class.achieved]="obj.achieved">
              <mat-card-header>
                <mat-icon mat-card-avatar [style.color]="obj.achieved ? 'var(--success)' : 'var(--text-muted)'">
                  {{ obj.achieved ? 'check_circle' : 'radio_button_unchecked' }}
                </mat-icon>
                <mat-card-title>{{ obj.ref_id }}</mat-card-title>
                <mat-card-subtitle>{{ obj.objective_type | uppercase }} — {{ obj.points }} pts</mat-card-subtitle>
              </mat-card-header>
              <mat-card-content>
                <p>{{ obj.description }}</p>
                @if (obj.achieved) {
                  <p class="meta">Achieved: {{ obj.achieved_at | date:'medium' }}</p>
                  @if (obj.evidence) { <p class="meta">Evidence: {{ obj.evidence }}</p> }
                }
              </mat-card-content>
              <mat-card-actions>
                @if (!obj.achieved) {
                  <button mat-button color="primary" (click)="ackObjective(obj.ref_id)">
                    <mat-icon>check</mat-icon> Acknowledge
                  </button>
                }
              </mat-card-actions>
            </mat-card>
          }
        </div>

        <div class="mt-3">
          <button mat-raised-button color="primary" (click)="generateAAR()">
            <mat-icon>assessment</mat-icon> Generate AAR
          </button>
          @if (aar()) {
            <button mat-button (click)="viewAARHtml()" class="ml-2">
              <mat-icon>open_in_new</mat-icon> View HTML Report
            </button>
          }
        </div>

        @if (aar()) {
          <h2 class="mt-3">After Action Report</h2>
          <mat-card>
            <mat-card-content>
              <pre style="max-height:400px;overflow:auto;font-size:13px;">{{ prettyReport() }}</pre>
            </mat-card-content>
          </mat-card>
        }
      }
    </div>
  `,
  styles: [`
    .achieved { border-left: 4px solid var(--success); }
    .meta { color: var(--text-muted); font-size: 13px; }
    :host > .page-container > mat-form-field { width: 100%; max-width: 500px; display: block; margin-bottom: 16px; }
  `],
})
export class ScoringComponent implements OnInit {
  exercises = signal<Exercise[]>([]);
  selectedExerciseId = '';
  selectedExercise = signal<Exercise | null>(null);
  objectives = signal<Objective[]>([]);
  aar = signal<AAR | null>(null);

  constructor(
    private api: ApiService,
    private notify: NotificationService,
    private sanitizer: DomSanitizer,
  ) {}

  ngOnInit(): void { this.api.listExercises().subscribe(e => this.exercises.set(e)); }

  scorePercent(): number {
    const ex = this.selectedExercise();
    if (!ex || !ex.max_score) return 0;
    return Math.round((ex.total_score / ex.max_score) * 100);
  }

  prettyReport(): string {
    const a = this.aar();
    if (!a) return '';
    try { return JSON.stringify(JSON.parse(a.report_json), null, 2); }
    catch { return a.report_json; }
  }

  loadExercise(): void {
    if (!this.selectedExerciseId) return;
    this.api.getExercise(this.selectedExerciseId).subscribe(e => this.selectedExercise.set(e));
    this.api.listObjectives(this.selectedExerciseId).subscribe(o => this.objectives.set(o));
    this.api.getAAR(this.selectedExerciseId).subscribe({
      next: a => this.aar.set(a),
      error: () => this.aar.set(null),
    });
  }

  ackObjective(refId: string): void {
    this.api.ackObjective(this.selectedExerciseId, refId, 'Manual acknowledgement').subscribe({
      next: () => { this.notify.success('Objective acknowledged'); this.loadExercise(); },
      error: () => this.notify.error('Failed to acknowledge'),
    });
  }

  generateAAR(): void {
    this.api.generateAAR(this.selectedExerciseId).subscribe({
      next: a => { this.aar.set(a); this.notify.success('AAR generated'); },
      error: () => this.notify.error('AAR generation failed'),
    });
  }

  viewAARHtml(): void {
    window.open(`/api/exercises/${this.selectedExerciseId}/aar/html`, '_blank');
  }
}