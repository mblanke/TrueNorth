import { Component, OnInit, signal, inject } from '@angular/core';
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
import { ActivatedRoute } from '@angular/router';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { AuthService } from '@core/services/auth.service';
import { Exercise, ExerciseSummary, Objective, AAR } from '@core/models';

@Component({
  selector: 'tn-scoring',
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

      <section class="review-selector">
      <div><h2>Start with an exercise</h2><p>Review its objectives, supporting evidence, and after-action report together.</p></div>
      <mat-form-field appearance="outline" subscriptSizing="dynamic">
        <mat-label>Select Exercise</mat-label>
        <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedExerciseId" (selectionChange)="loadExercise()">
          @for (ex of exercises(); track ex.id) {
            <mat-option [value]="ex.id">{{ ex.name }} ({{ ex.state }})</mat-option>
          }
        </mat-select>
      </mat-form-field>
      </section>

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
                @if (!obj.achieved && canAck()) {
                  <button mat-button color="primary" (click)="ackObjective(obj.ref_id)">
                    <mat-icon>check</mat-icon> Acknowledge
                  </button>
                }
              </mat-card-actions>
            </mat-card>
          }
        </div>

        <div class="mt-3 aar-actions">
          <button mat-raised-button color="primary" (click)="generateAAR()">
            <mat-icon>assessment</mat-icon> {{ aar() ? 'Regenerate AAR' : 'Generate AAR' }}
          </button>
          @if (aar()) {
            <button mat-stroked-button (click)="viewAARHtml()" data-testid="aar-view">
              <mat-icon>article</mat-icon> View report
            </button>
            <button mat-stroked-button (click)="downloadAARPdf()" data-testid="aar-pdf">
              <mat-icon>picture_as_pdf</mat-icon> Download PDF
            </button>
          }
        </div>

        @if (aarHtml()) {
          <h2 class="mt-3">After Action Report</h2>
          <!-- Sandboxed with no permissions: the report's own CSP forbids scripts as well. -->
          <iframe class="aar-frame" sandbox="" title="After-action report" [srcdoc]="aarHtml()"></iframe>
        }
      }
    </div>
  `,
  styles: [`
    .review-selector { display: grid; grid-template-columns: 1fr minmax(240px, 400px); gap: 24px; align-items: center; padding: 24px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); }
    .review-selector h2 { font-size: 17px; font-weight: 600; margin-bottom: 6px; }
    .review-selector p { color: var(--text-muted); font-size: 13px; margin: 0; }
    @media (max-width: 800px) { .review-selector { grid-template-columns: minmax(0, 1fr); } }
    .achieved { border-left: 4px solid var(--success); }
    .meta { color: var(--text-muted); font-size: 13px; }
    .aar-actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .aar-frame { width: 100%; height: 70vh; border: 1px solid var(--border); border-radius: 8px; background: #fff; }
    :host > .page-container > mat-form-field { width: 100%; max-width: 500px; display: block; margin-bottom: 16px; }
  `],
})
export class ScoringComponent implements OnInit {
  private api = inject(ApiService);
  private notify = inject(NotificationService);
  private sanitizer = inject(DomSanitizer);
  private auth = inject(AuthService);

  /** Students see objectives but cannot award them (objective:ack). */
  readonly canAck = this.auth.canAcknowledgeObjectives;

  private route = inject(ActivatedRoute);

  exercises = signal<ExerciseSummary[]>([]);
  selectedExerciseId = '';
  selectedExercise = signal<Exercise | null>(null);
  objectives = signal<Objective[]>([]);
  aar = signal<AAR | null>(null);
  /** The report page, fetched through the API client (so it carries the bearer token) and
   *  shown in a sandboxed iframe. Trusted only for that srcdoc: the API escapes every value. */
  aarHtml = signal<SafeHtml | null>(null);

  ngOnInit(): void {
    this.api.listExercises().subscribe(e => this.exercises.set(e));
    // /scoring?exercise=<id> opens straight onto one exercise's review.
    const preselect = this.route.snapshot.queryParamMap.get('exercise');
    if (preselect) {
      this.selectedExerciseId = preselect;
      this.loadExercise();
    }
  }

  scorePercent(): number {
    const ex = this.selectedExercise();
    if (!ex || !ex.max_score) return 0;
    return Math.round((ex.total_score / ex.max_score) * 100);
  }

  loadExercise(): void {
    if (!this.selectedExerciseId) return;
    this.aarHtml.set(null);
    this.api.getExercise(this.selectedExerciseId).subscribe(e => this.selectedExercise.set(e));
    this.api.listObjectives(this.selectedExerciseId).subscribe(o => this.objectives.set(o));
    this.api.getAAR(this.selectedExerciseId).subscribe({
      next: a => { this.aar.set(a); this.viewAARHtml(); },
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
      next: a => {
        this.aar.set(a);
        this.notify.success('AAR generated');
        this.viewAARHtml();
      },
      error: () => this.notify.error('AAR generation failed'),
    });
  }

  /** Was window.open on /api/.../aar/html: a plain navigation sends no bearer token, so
   *  outside dev mode it opened a 401. */
  viewAARHtml(): void {
    this.api.getAARHtml(this.selectedExerciseId).subscribe({
      next: html => this.aarHtml.set(this.sanitizer.bypassSecurityTrustHtml(html)),
      error: (err) => this.notify.error(err?.error?.detail || 'Could not load the report'),
    });
  }

  downloadAARPdf(): void {
    const id = this.selectedExerciseId;
    this.api.getAARPdf(id).subscribe({
      next: blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `aar-${id}.pdf`;
        a.click();
        window.URL.revokeObjectURL(url);
      },
      error: () => this.notify.error('PDF download failed. Generate the AAR first.'),
    });
  }
}
