import {
  Component, ElementRef, OnDestroy, OnInit, ViewChild, effect, inject, signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { FormsModule } from '@angular/forms';
import { ApiService } from '@core/services/api.service';
import { ThemeService } from '@core/services/theme.service';
import { AuthService } from '@core/services/auth.service';
import { tnChartColors, tnCartesianBase } from '../../shared/charts/echarts-theme';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { CompetencyHeatmapComponent } from './competency-heatmap.component';

/** Canonical low-to-high ordering for proficiency buckets on the bar chart. */
const PROFICIENCY_ORDER = ['novice', 'beginner', 'intermediate', 'advanced', 'expert'];

interface Competency {
  id: string;
  code: string;
  name: string;
  framework: string;
  category: string;
  level: number;
}

interface CompetencyAssertion {
  competency_id: string;
  proficiency: string;
  assessed_at: string;
  source: string;
}

interface CompetencyProfile {
  assertions: CompetencyAssertion[];
  by_framework: Record<string, number>;
  by_proficiency: Record<string, number>;
}

interface SkillGap {
  competency: Competency;
  current_proficiency: string | null;
  required_proficiency: string;
  recommended_courses: string[];
}

@Component({
  selector: 'tn-competency',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatTabsModule, MatTableModule, MatSelectModule, MatFormFieldModule, MatProgressBarModule,
    CompetencyHeatmapComponent, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">psychology</mat-icon>
          <div>
            <h1>Competency Framework</h1>
            <p class="subtitle">NICE SP 800-181 workforce framework mapping, skill assessment, and gap analysis.</p>
          </div>
        </div>
      </div>

      <mat-tab-group>
        <mat-tab label="My Profile">
          <div class="tab-content mt-2">
            @if (profile()) {
              <div class="stats-row">
                @for (entry of frameworkEntries(); track entry[0]) {
                  <mat-card class="stat-card">
                    <mat-card-content>
                      <div class="stat-value">{{ entry[1] }}</div>
                      <div class="stat-label">{{ entry[0] | uppercase }}</div>
                    </mat-card-content>
                  </mat-card>
                }
              </div>

              <mat-card class="chart-card mt-2">
                <mat-card-header>
                  <mat-card-title>Assertions by Framework</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <div #fwChart class="chart-container"></div>
                </mat-card-content>
              </mat-card>

              <h3 class="mt-2">Proficiency Distribution</h3>
              <div class="stats-row">
                @for (entry of proficiencyEntries(); track entry[0]) {
                  <mat-card>
                    <mat-card-content>
                      <div class="stat-value">{{ entry[1] }}</div>
                      <div class="stat-label">{{ entry[0] }}</div>
                    </mat-card-content>
                  </mat-card>
                }
              </div>

              <mat-card class="chart-card mt-2">
                <mat-card-header>
                  <mat-card-title>Assertions by Proficiency Level</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <div #profChart class="chart-container"></div>
                </mat-card-content>
              </mat-card>
            } @else {
              <mat-card><mat-card-content>No competency data yet. Complete assessments to build your profile.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Skill Gaps">
          <div class="tab-content mt-2">
            <div class="gap-controls">
              <mat-form-field appearance="outline" class="role-select">
                <mat-label>Target Work Role</mat-label>
                <mat-select [(value)]="selectedRole" panelClass="tn-select-panel" (selectionChange)="loadSkillGaps()">
                  @for (role of niceRoles; track role) {
                    <mat-option [value]="role">{{ role }}</mat-option>
                  }
                </mat-select>
              </mat-form-field>
            </div>

            @if (skillGaps().length) {
              <div class="card-grid">
                @for (gap of skillGaps(); track gap.competency.id) {
                  <mat-card class="gap-card">
                    <mat-card-header>
                      <mat-icon mat-card-avatar color="warn">trending_up</mat-icon>
                      <mat-card-title>{{ gap.competency.name }}</mat-card-title>
                      <mat-card-subtitle>{{ gap.competency.code }} &middot; {{ gap.competency.framework }}</mat-card-subtitle>
                    </mat-card-header>
                    <mat-card-content>
                      <p>Current: <strong>{{ gap.current_proficiency || 'None' }}</strong> &rarr; Required: <strong>{{ gap.required_proficiency }}</strong></p>
                      @if (gap.recommended_courses.length) {
                        <p class="rec">Recommended: {{ gap.recommended_courses.join(', ') }}</p>
                      }
                    </mat-card-content>
                  </mat-card>
                }
              </div>
            } @else {
              <tn-empty-state icon="track_changes" title="No skill gaps to show"
                              message="Select a target work role to see gaps, or you may already meet the requirements for this role." />
            }
          </div>
        </mat-tab>

        <mat-tab label="Framework Browser">
          <div class="tab-content mt-2">
            <div class="page-header">
              <button mat-raised-button color="primary" (click)="importNice()">
                <mat-icon>download</mat-icon> Import NICE Framework
              </button>
            </div>

            @if (competencies().length) {
              <table mat-table [dataSource]="competencies()" class="full-width mt-2">
                <ng-container matColumnDef="code">
                  <th mat-header-cell *matHeaderCellDef>Code</th>
                  <td mat-cell *matCellDef="let c">{{ c.code }}</td>
                </ng-container>
                <ng-container matColumnDef="name">
                  <th mat-header-cell *matHeaderCellDef>Name</th>
                  <td mat-cell *matCellDef="let c">{{ c.name }}</td>
                </ng-container>
                <ng-container matColumnDef="framework">
                  <th mat-header-cell *matHeaderCellDef>Framework</th>
                  <td mat-cell *matCellDef="let c">{{ c.framework }}</td>
                </ng-container>
                <ng-container matColumnDef="category">
                  <th mat-header-cell *matHeaderCellDef>Category</th>
                  <td mat-cell *matCellDef="let c">{{ c.category }}</td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="frameworkColumns"></tr>
                <tr mat-row *matRowDef="let row; columns: frameworkColumns"></tr>
              </table>
            } @else {
              <mat-card class="mt-2"><mat-card-content>No competencies loaded. Click "Import NICE Framework" to seed the database.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Heatmap">
          <div class="tab-content mt-2">
            <tn-competency-heatmap></tn-competency-heatmap>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 16px; }
    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stats-row mat-card { flex: 1; min-width: 140px; text-align: center; }
        .stat-label { text-transform: capitalize; }
    .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .chart-card mat-card-title { font-size: 15px; }
    .chart-container { width: 100%; height: 260px; }
    .gap-controls { margin-bottom: 16px; }
    .role-select { min-width: 300px; }
    .full-width { width: 100%; }
    .tab-content { padding: 16px 0; }
    .page-header { display: flex; justify-content: flex-end; }
    .mt-2 { margin-top: 16px; }
    .rec { font-size: 12px; color: var(--text-secondary); }
  `],
})
export class CompetencyComponent implements OnInit, OnDestroy {
  @ViewChild('fwChart') fwChart?: ElementRef<HTMLDivElement>;
  @ViewChild('profChart') profChart?: ElementRef<HTMLDivElement>;

  competencies = signal<Competency[]>([]);
  profile = signal<CompetencyProfile | null>(null);
  skillGaps = signal<SkillGap[]>([]);
  frameworkColumns = ['code', 'name', 'framework', 'category'];
  selectedRole = '';
  private readonly auth = inject(AuthService);
  // Was hardcoded to the dev-admin UUID — see my-progress.component.ts.
  private get userId(): string {
    return this.auth.userId() ?? '';
  }

  private fwInstance: any = null;
  private profInstance: any = null;
  private resizeObserver: ResizeObserver | null = null;
  private readonly theme = inject(ThemeService);

  niceRoles = [
    'SP-RSK-001', 'SP-RSK-002', 'SP-DEV-001', 'SP-DEV-002', 'SP-ARC-001', 'SP-ARC-002',
    'SP-SRP-001', 'SP-TST-001', 'SP-SYS-001', 'SP-SYS-002',
    'OM-DTA-001', 'OM-DTA-002', 'OM-KMG-001', 'OM-STS-001', 'OM-NET-001', 'OM-ADM-001',
    'OV-LGA-001', 'OV-LGA-002',
  ];

  constructor(private api: ApiService) {
    // ECharts snapshots CSS variables at option-build time, so both a theme
    // switch and a fresh profile load must (re)build the options. The chart
    // containers live behind an @if, so defer a tick for them to render.
    effect(() => {
      this.theme.activeTheme();
      const p = this.profile();
      if (!p) return;
      setTimeout(() => this.renderCharts(p));
    });
  }

  frameworkEntries(): [string, number][] {
    const p = this.profile();
    return p ? Object.entries(p.by_framework) : [];
  }

  proficiencyEntries(): [string, number][] {
    const p = this.profile();
    return p ? Object.entries(p.by_proficiency) : [];
  }

  ngOnInit() {
    this.loadFramework();
    this.loadProfile();
  }

  ngOnDestroy() {
    this.resizeObserver?.disconnect();
    this.fwInstance?.dispose();
    this.profInstance?.dispose();
  }

  private async renderCharts(p: CompetencyProfile) {
    const fwEl = this.fwChart?.nativeElement;
    const profEl = this.profChart?.nativeElement;
    if (!fwEl || !profEl) return;

    // Dynamic import keeps ECharts out of the initial bundle (same pattern
    // as the heatmap component alongside this page).
    const echarts = await import('echarts');
    if (!this.fwInstance) {
      this.fwInstance = echarts.init(fwEl);
      this.profInstance = echarts.init(profEl);
      this.resizeObserver = new ResizeObserver(() => {
        this.fwInstance?.resize();
        this.profInstance?.resize();
      });
      this.resizeObserver.observe(fwEl);
      this.resizeObserver.observe(profEl);
    }

    const c = tnChartColors();
    const base = tnCartesianBase(c);

    const fw = Object.entries(p.by_framework);
    this.fwInstance.setOption({
      ...base,
      xAxis: {
        type: 'value',
        axisLabel: { color: c.textMuted },
        splitLine: { lineStyle: { color: c.border } },
      },
      yAxis: {
        type: 'category',
        data: fw.map(e => e[0].toUpperCase()),
        axisLabel: { color: c.textMuted },
        axisLine: { lineStyle: { color: c.border } },
      },
      series: [{
        type: 'bar',
        data: fw.map(e => e[1]),
        barMaxWidth: 26,
        itemStyle: { color: c.accent, borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: c.text },
      }],
    }, true);

    const prof = Object.entries(p.by_proficiency).sort((a, b) => {
      const ia = PROFICIENCY_ORDER.indexOf(a[0].toLowerCase());
      const ib = PROFICIENCY_ORDER.indexOf(b[0].toLowerCase());
      return (ia === -1 ? PROFICIENCY_ORDER.length : ia) - (ib === -1 ? PROFICIENCY_ORDER.length : ib);
    });
    this.profInstance.setOption({
      ...base,
      xAxis: {
        type: 'category',
        data: prof.map(e => e[0]),
        axisLabel: { color: c.textMuted },
        axisLine: { lineStyle: { color: c.border } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: c.textMuted },
        splitLine: { lineStyle: { color: c.border } },
      },
      series: [{
        type: 'bar',
        data: prof.map(e => e[1]),
        barMaxWidth: 40,
        itemStyle: { color: c.accent, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: c.text },
      }],
    }, true);
  }

  loadFramework() {
    this.api.get<Competency[]>('/competency/frameworks').subscribe({
      next: c => this.competencies.set(c || []),
      error: () => this.competencies.set([]),
    });
  }

  loadProfile() {
    this.api.get<CompetencyProfile>(`/competency/users/${this.userId}/profile`).subscribe({
      next: p => this.profile.set(p),
      error: () => this.profile.set(null),
    });
  }

  loadSkillGaps() {
    if (!this.selectedRole) return;
    this.api.get<SkillGap[]>(`/competency/users/${this.userId}/skill-gaps?target_role=${this.selectedRole}`).subscribe({
      next: g => this.skillGaps.set(g || []),
      error: () => this.skillGaps.set([]),
    });
  }

  importNice() {
    this.api.post('/competency/frameworks/import-nice', {}).subscribe({
      next: () => { alert('NICE framework imported!'); this.loadFramework(); },
      error: (err: any) => alert(err.error?.detail || 'Import failed'),
    });
  }
}