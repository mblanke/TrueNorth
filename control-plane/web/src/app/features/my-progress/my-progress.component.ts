import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { ApiService } from '@core/services/api.service';

interface TranscriptEntry {
  source: string;
  activity_type: string;
  title: string;
  score: number | null;
  grade: string | null;
  completed_at: string | null;
  competencies_earned: string[];
}

interface Certification {
  cert_name: string;
  issuer: string;
  credential_id: string;
  issued_at: string;
  expires_at: string | null;
  status: string;
}

interface Transcript {
  entries: TranscriptEntry[];
  certifications: Certification[];
  total_hours: number;
}

@Component({
  selector: 'tn-my-progress',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatButtonModule, MatIconModule,
    MatTabsModule, MatTableModule, MatProgressBarModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">trending_up</mat-icon>
          <div>
            <h1>My Progress</h1>
            <p class="subtitle">Unified transcript across all training sources.</p>
          </div>
        </div>
      </div>

      <div class="stats-row">
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">timer</mat-icon>
            <div class="stat-value">{{ transcript()?.total_hours || 0 }}</div>
            <div class="stat-label">Training Hours</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">school</mat-icon>
            <div class="stat-value">{{ transcript()?.entries?.length || 0 }}</div>
            <div class="stat-label">Activities</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">workspace_premium</mat-icon>
            <div class="stat-value">{{ transcript()?.certifications?.length || 0 }}</div>
            <div class="stat-label">Certifications</div>
          </mat-card-content>
        </mat-card>
      </div>

      <mat-tab-group class="mt-2">
        <mat-tab label="Transcript">
          <div class="tab-content">
            @if (transcript()?.entries?.length) {
              <table mat-table [dataSource]="transcript()!.entries" class="full-width">
                <ng-container matColumnDef="source">
                  <th mat-header-cell *matHeaderCellDef>Source</th>
                  <td mat-cell *matCellDef="let e">
                    <span class="source-badge" [attr.data-source]="e.source">{{ e.source }}</span>
                  </td>
                </ng-container>
                <ng-container matColumnDef="title">
                  <th mat-header-cell *matHeaderCellDef>Activity</th>
                  <td mat-cell *matCellDef="let e">{{ e.title }}</td>
                </ng-container>
                <ng-container matColumnDef="type">
                  <th mat-header-cell *matHeaderCellDef>Type</th>
                  <td mat-cell *matCellDef="let e">{{ e.activity_type }}</td>
                </ng-container>
                <ng-container matColumnDef="score">
                  <th mat-header-cell *matHeaderCellDef>Score</th>
                  <td mat-cell *matCellDef="let e">{{ e.grade || (e.score != null ? e.score + '%' : '-') }}</td>
                </ng-container>
                <ng-container matColumnDef="completed">
                  <th mat-header-cell *matHeaderCellDef>Completed</th>
                  <td mat-cell *matCellDef="let e">{{ e.completed_at ? (e.completed_at | date:'mediumDate') : '-' }}</td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="transcriptColumns"></tr>
                <tr mat-row *matRowDef="let row; columns: transcriptColumns"></tr>
              </table>
            } @else {
              <mat-card><mat-card-content>No transcript entries yet. Complete courses or exercises to build your record.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Certifications">
          <div class="tab-content">
            <div class="card-grid">
              @for (cert of transcript()?.certifications || []; track cert.credential_id) {
                <mat-card>
                  <mat-card-header>
                    <mat-icon mat-card-avatar color="accent">verified</mat-icon>
                    <mat-card-title>{{ cert.cert_name }}</mat-card-title>
                    <mat-card-subtitle>{{ cert.issuer }}</mat-card-subtitle>
                  </mat-card-header>
                  <mat-card-content>
                    <p>Issued: {{ cert.issued_at | date:'mediumDate' }}</p>
                    @if (cert.expires_at) {
                      <p>Expires: {{ cert.expires_at | date:'mediumDate' }}</p>
                    }
                    <p>Status: <strong>{{ cert.status }}</strong></p>
                  </mat-card-content>
                </mat-card>
              } @empty {
                <mat-card><mat-card-content>No certifications recorded yet.</mat-card-content></mat-card>
              }
            </div>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 16px; }
    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stats-row mat-card { flex: 1; min-width: 160px; text-align: center; }
            .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
    .full-width { width: 100%; }
    .tab-content { padding: 16px 0; }
    .mt-2 { margin-top: 16px; }
    .source-badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
    .source-badge[data-source="truenorth"] { background: var(--primary); color: white; }
    .source-badge[data-source="moodle"] { background: #f98012; color: white; }
    .source-badge[data-source="external"] { background: #6366f1; color: white; }
  `],
})
export class MyProgressComponent implements OnInit {
  transcript = signal<Transcript | null>(null);
  transcriptColumns = ['source', 'title', 'type', 'score', 'completed'];
  private userId = '00000000-0000-0000-0000-000000000001'; // TODO: get from auth

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.loadTranscript();
  }

  loadTranscript() {
    this.api.get<Transcript>(`/users/${this.userId}/transcript`).subscribe({
      next: t => this.transcript.set(t),
      error: () => this.transcript.set({ entries: [], certifications: [], total_hours: 0 }),
    });
  }
}