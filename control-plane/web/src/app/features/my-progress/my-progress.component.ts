import { Component, OnInit, effect, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { ThemeService } from '@core/services/theme.service';
import { NgxEchartsDirective, provideEcharts } from 'ngx-echarts';
import type { EChartsOption } from 'echarts';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';
import { EnterStaggerDirective } from '../../shared/motion';
import { tnChartColors } from '../../shared/charts/echarts-theme';

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

interface ProgressSummary {
  avg_score: number;
  total_exercises: number;
  strongest_areas: string[];
  weakest_areas: string[];
  competency_trend: { category: string; avg_delta: number; count: number }[];
}

interface Recommendation {
  id: string;
  summary: string;
  strengths: string[];
  gaps: string[];
  recommendations: { area?: string; action?: string }[];
  next_milestone: string;
  generated_at: string;
}

interface AutoAssessment {
  id: string;
  exercise_id: string;
  raw_score: number;
  max_score: number;
  competency_mappings: { category: string; delta: number }[];
  assessed_at: string;
}

@Component({
  selector: 'tn-my-progress',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatButtonModule, MatIconModule,
    MatTabsModule, MatTableModule, MatProgressBarModule,
    MatChipsModule, MatTooltipModule, LottieIconComponent, EnterStaggerDirective,
    NgxEchartsDirective,
  ],
  providers: [provideEcharts()],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <tn-lottie name="progress-orbit" [size]="64" />
          <div>
            <h1>My Progress</h1>
            <p class="subtitle">Unified transcript across all training sources.</p>
          </div>
        </div>
      </div>

      <div class="stats-row" tnEnterStagger>
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
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">psychology</mat-icon>
            <div class="stat-value">{{ progressSummary()?.total_exercises ? (progressSummary()!.avg_score | number:'1.0-0') + '%' : '-' }}</div>
            <div class="stat-label">Avg Score</div>
          </mat-card-content>
        </mat-card>
      </div>

      @if (radarOption(); as radar) {
        <mat-card class="radar-card mt-2">
          <h2 class="radar-title"><mat-icon>track_changes</mat-icon> Capability Radar</h2>
          <p class="radar-sub">Demonstrated proficiency by competency category — from quizzes and range exercises</p>
          <div echarts [options]="radar" class="radar-chart"></div>
        </mat-card>
      }

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
                  <td mat-cell *matCellDef="let e">{{ e.grade || (e.score !== null ? e.score + '%' : '-') }}</td>
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
                      <p class="expires-line">
                        Expires: {{ cert.expires_at | date:'mediumDate' }}
                        @if (certExpiry(cert) === 'expired') {
                          <span class="status-chip failed">expired</span>
                        } @else if (certExpiry(cert) === 'expiring') {
                          <span class="status-chip sev-medium">expires soon</span>
                        }
                      </p>
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

        <mat-tab label="AI Insights">
          <div class="tab-content">
            <div class="section-header">
              <h3>Learning Recommendations</h3>
              <button mat-stroked-button color="primary" (click)="generateRecommendation()" [disabled]="generatingRec()">
                <mat-icon>auto_awesome</mat-icon>
                {{ generatingRec() ? 'Generating...' : 'Generate Recommendation' }}
              </button>
            </div>

            @if (progressSummary(); as ps) {
              <div class="insights-grid">
                @if (ps.strongest_areas.length) {
                  <mat-card>
                    <mat-card-header><mat-card-title>Strengths</mat-card-title></mat-card-header>
                    <mat-card-content>
                      <mat-chip-set>
                        @for (s of ps.strongest_areas; track s) {
                          <mat-chip color="primary" highlighted>{{ s }}</mat-chip>
                        }
                      </mat-chip-set>
                    </mat-card-content>
                  </mat-card>
                }
                @if (ps.weakest_areas.length) {
                  <mat-card>
                    <mat-card-header><mat-card-title>Areas to Improve</mat-card-title></mat-card-header>
                    <mat-card-content>
                      <mat-chip-set>
                        @for (g of ps.weakest_areas; track g) {
                          <mat-chip color="warn" highlighted>{{ g }}</mat-chip>
                        }
                      </mat-chip-set>
                    </mat-card-content>
                  </mat-card>
                }
              </div>

              @if (trendOption(); as trend) {
                <mat-card class="trend-card">
                  <h3 class="trend-title"><mat-icon>swap_vert</mat-icon> Competency Trend</h3>
                  <p class="trend-sub">Average proficiency delta by category — gains toward the right, losses toward the left (n = assessments counted)</p>
                  <div echarts [options]="trend" class="trend-chart"></div>
                </mat-card>
              }
            }

            @for (rec of recommendations(); track rec.id) {
              <mat-card class="rec-card">
                <mat-card-header>
                  <mat-icon mat-card-avatar color="accent">lightbulb</mat-icon>
                  <mat-card-title>{{ rec.summary || 'Recommendation' }}</mat-card-title>
                  <mat-card-subtitle>{{ rec.generated_at | date:'medium' }}</mat-card-subtitle>
                </mat-card-header>
                <mat-card-content>
                  @if (rec.recommendations.length) {
                    <h4>Recommended Actions</h4>
                    <ul>
                      @for (r of rec.recommendations; track r) {
                        <li><strong>{{ r.area }}</strong>: {{ r.action }}</li>
                      }
                    </ul>
                  }
                  @if (rec.next_milestone) {
                    <p><strong>Next milestone:</strong> {{ rec.next_milestone }}</p>
                  }
                  @if (rec.gaps.length) {
                    <h4>Focus Areas</h4>
                    <mat-chip-set>
                      @for (g of rec.gaps; track g) {
                        <mat-chip>{{ g }}</mat-chip>
                      }
                    </mat-chip-set>
                  }
                </mat-card-content>
              </mat-card>
            } @empty {
              <mat-card class="mt-1"><mat-card-content>No recommendations yet. Click "Generate Recommendation" to get AI-powered learning guidance.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Assessments">
          <div class="tab-content">
            @for (a of assessments(); track a.id) {
              <mat-card class="assessment-card">
                <mat-card-header>
                  <mat-icon mat-card-avatar [color]="(a.max_score > 0 ? a.raw_score / a.max_score * 100 : 0) >= 70 ? 'primary' : 'warn'">assessment</mat-icon>
                  <mat-card-title>Score: {{ a.raw_score }}/{{ a.max_score }}</mat-card-title>
                  <mat-card-subtitle>{{ a.assessed_at | date:'medium' }}</mat-card-subtitle>
                </mat-card-header>
                <mat-card-content>
                  @if (a.competency_mappings.length) {
                    <div class="competency-bars">
                      @for (m of a.competency_mappings; track m.category) {
                        <div class="competency-row">
                          <span class="competency-label" [matTooltip]="m.category">{{ m.category }}</span>
                          <mat-progress-bar mode="determinate" [value]="m.delta > 0 ? m.delta : 0"></mat-progress-bar>
                          <span class="competency-score">{{ m.delta > 0 ? '+' : '' }}{{ m.delta }}</span>
                        </div>
                      }
                    </div>
                  }
                </mat-card-content>
              </mat-card>
            } @empty {
              <mat-card><mat-card-content>No auto-assessments yet. Complete exercises to receive AI competency assessments.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 16px; }
    .radar-card { padding: 18px 18px 6px; }
    .radar-title {
      display: flex; align-items: center; gap: 8px; margin: 0;
      font-family: var(--font-display); font-size: 16px; font-weight: 700; color: var(--text-primary);
    }
    .radar-title mat-icon { color: var(--accent); }
    .radar-sub { font-size: 12px; color: var(--text-muted); margin: 4px 0 0; }
    .radar-chart { height: 320px; width: 100%; }
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
    .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .section-header h3 { margin: 0; }
    .insights-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    .trend-card { padding: 18px 18px 6px; margin-bottom: 16px; }
    .trend-title {
      display: flex; align-items: center; gap: 8px; margin: 0;
      font-family: var(--font-display); font-size: 15px; font-weight: 700; color: var(--text-primary);
    }
    .trend-title mat-icon { color: var(--accent); }
    .trend-sub { font-size: 12px; color: var(--text-muted); margin: 4px 0 0; }
    .trend-chart { height: 260px; width: 100%; }
    .expires-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .rec-card, .assessment-card { margin-bottom: 12px; }
    .rec-card ul { padding-left: 20px; }
    .mt-1 { margin-top: 8px; }
    .competency-bars { display: flex; flex-direction: column; gap: 8px; }
    .competency-row { display: flex; align-items: center; gap: 8px; }
    .competency-label { min-width: 140px; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .competency-score { min-width: 40px; text-align: right; font-weight: 500; }
  `],
})
export class MyProgressComponent implements OnInit {
  transcript = signal<Transcript | null>(null);
  progressSummary = signal<ProgressSummary | null>(null);
  recommendations = signal<Recommendation[]>([]);
  assessments = signal<AutoAssessment[]>([]);
  generatingRec = signal(false);
  transcriptColumns = ['source', 'title', 'type', 'score', 'completed'];
  private userId = '00000000-0000-0000-0000-000000000001'; // TODO: get from auth

  radarOption = signal<EChartsOption | null>(null);
  trendOption = signal<EChartsOption | null>(null);

  private lastAssertions: any[] | null = null;
  private readonly theme = inject(ThemeService);

  constructor(private api: ApiService, private notify: NotificationService) {
    // Chart options snapshot CSS variables when built, so a theme switch must
    // rebuild them. Reading progressSummary() here also builds the trend chart
    // the first time the data lands.
    effect(() => {
      this.theme.activeTheme();
      const ps = this.progressSummary();
      if (ps?.competency_trend?.length) this.buildTrend(ps.competency_trend);
      if (this.lastAssertions) this.buildRadar(this.lastAssertions);
    });
  }

  certExpiry(cert: Certification): 'expired' | 'expiring' | null {
    if (!cert.expires_at) return null;
    const expires = new Date(cert.expires_at).getTime();
    if (Number.isNaN(expires)) return null;
    const now = Date.now();
    if (expires < now) return 'expired';
    if (expires - now <= 60 * 24 * 60 * 60 * 1000) return 'expiring';
    return null;
  }

  ngOnInit() {
    this.loadTranscript();
    this.loadProgress();
    this.loadRecommendations();
    this.loadCapabilityRadar();
    this.loadAssessments();
  }

  loadTranscript() {
    this.api.get<Transcript>(`/users/${this.userId}/transcript`).subscribe({
      next: t => this.transcript.set(t),
      error: () => this.transcript.set({ entries: [], certifications: [], total_hours: 0 }),
    });
  }

  loadProgress() {
    this.api.get<ProgressSummary>(`/adaptive/users/${this.userId}/progress`).subscribe({
      next: p => this.progressSummary.set(p),
      error: () => {},
    });
  }

  loadRecommendations() {
    this.api.get<Recommendation[]>(`/adaptive/users/${this.userId}/recommendations`).subscribe({
      next: r => this.recommendations.set(r),
      error: () => {},
    });
  }

  loadAssessments() {
    this.api.get<AutoAssessment[]>(`/adaptive/users/${this.userId}/auto-assessments`).subscribe({
      next: a => this.assessments.set(a),
      error: () => {},
    });
  }

  loadCapabilityRadar() {
    this.api.getCompetencyProfile(this.userId).subscribe({
      next: profile => {
        const assertions = profile?.assertions || [];
        this.lastAssertions = assertions;
        this.buildRadar(assertions);
      },
      error: () => {},
    });
  }

  private buildTrend(trend: { category: string; avg_delta: number; count: number }[]) {
    const rows = [...trend].sort((a, b) => a.avg_delta - b.avg_delta);
    const c = tnChartColors();
    this.trendOption.set({
      textStyle: { color: c.textMuted, fontFamily: 'Inter, sans-serif' },
      grid: { left: 8, right: 48, top: 10, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: c.card,
        borderColor: c.border,
        textStyle: { color: c.text },
        formatter: (params: any) => {
          const p = Array.isArray(params) ? params[0] : params;
          const row = rows[p.dataIndex];
          const sign = row.avg_delta > 0 ? '+' : '';
          return `<b>${row.category}</b><br/>Avg delta: ${sign}${row.avg_delta}<br/>Assessments: ${row.count}`;
        },
      },
      xAxis: {
        type: 'value',
        axisLabel: { color: c.textMuted },
        splitLine: { lineStyle: { color: c.border } },
      },
      yAxis: {
        type: 'category',
        data: rows.map(r => r.category),
        axisLabel: { color: c.textMuted },
        axisLine: { lineStyle: { color: c.border } },
      },
      series: [{
        type: 'bar',
        barMaxWidth: 22,
        data: rows.map(r => ({
          value: r.avg_delta,
          itemStyle: {
            color: r.avg_delta >= 0 ? c.success : c.alert,
            borderRadius: r.avg_delta >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4],
          },
          label: { position: r.avg_delta >= 0 ? 'right' : 'left' },
        })),
        label: {
          show: true,
          color: c.text,
          formatter: (p: any) => {
            const row = rows[p.dataIndex];
            const sign = row.avg_delta > 0 ? '+' : '';
            return `${sign}${row.avg_delta} (n=${row.count})`;
          },
        },
      }],
    } as EChartsOption);
  }

  private buildRadar(assertions: any[]) {
    if (!assertions.length) return;
    const level: Record<string, number> = { novice: 1, beginner: 2, intermediate: 3, advanced: 4, expert: 5 };
    const byCategory = new Map<string, number[]>();
    for (const a of assertions) {
      const category = a.competency?.category || 'general';
      const scores = byCategory.get(category) ?? [];
      scores.push(level[a.proficiency] ?? 1);
      byCategory.set(category, scores);
    }
    const categories = [...byCategory.keys()].slice(0, 8);
    if (categories.length < 3) return; // radar needs at least 3 axes to read well
    const values = categories.map(cat => {
      const scores = byCategory.get(cat)!;
      return +(scores.reduce((s, v) => s + v, 0) / scores.length).toFixed(2);
    });

    const c = tnChartColors();
    this.radarOption.set({
      textStyle: { color: c.textMuted, fontFamily: 'Inter, sans-serif' },
      tooltip: { backgroundColor: c.card, borderColor: c.border, textStyle: { color: c.text } },
      radar: {
        indicator: categories.map(name => ({ name, max: 5 })),
        splitArea: { areaStyle: { color: ['transparent'] } },
        splitLine: { lineStyle: { color: c.border } },
        axisLine: { lineStyle: { color: c.border } },
        axisName: { color: c.textMuted, fontSize: 11 },
      },
      series: [{
        type: 'radar',
        data: [{
          value: values,
          name: 'Proficiency',
          areaStyle: { color: c.accent, opacity: 0.25 },
          lineStyle: { color: c.accent, width: 2 },
          itemStyle: { color: c.accentHover },
        }],
      }],
    } as EChartsOption);
  }

  generateRecommendation() {
    this.generatingRec.set(true);
    this.api.post(`/adaptive/users/${this.userId}/recommendations`, {}).subscribe({
      next: () => {
        this.notify.success('Recommendation generation started');
        this.generatingRec.set(false);
        setTimeout(() => this.loadRecommendations(), 3000);
      },
      error: () => {
        this.notify.error('Failed to generate recommendation');
        this.generatingRec.set(false);
      },
    });
  }
}