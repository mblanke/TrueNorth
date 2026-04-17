import { Component, OnInit, signal } from '@angular/core';
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
import { CompetencyHeatmapComponent } from './competency-heatmap.component';

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
    CompetencyHeatmapComponent,
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
              <mat-card><mat-card-content>Select a target role to see skill gaps, or you may have no gaps for this role.</mat-card-content></mat-card>
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
    .gap-controls { margin-bottom: 16px; }
    .role-select { min-width: 300px; }
    .full-width { width: 100%; }
    .tab-content { padding: 16px 0; }
    .page-header { display: flex; justify-content: flex-end; }
    .mt-2 { margin-top: 16px; }
    .rec { font-size: 12px; color: var(--text-secondary); }
  `],
})
export class CompetencyComponent implements OnInit {
  competencies = signal<Competency[]>([]);
  profile = signal<CompetencyProfile | null>(null);
  skillGaps = signal<SkillGap[]>([]);
  frameworkColumns = ['code', 'name', 'framework', 'category'];
  selectedRole = '';
  private userId = '00000000-0000-0000-0000-000000000001'; // TODO: auth

  niceRoles = [
    'SP-RSK-001', 'SP-RSK-002', 'SP-DEV-001', 'SP-DEV-002', 'SP-ARC-001', 'SP-ARC-002',
    'SP-SRP-001', 'SP-TST-001', 'SP-SYS-001', 'SP-SYS-002',
    'OM-DTA-001', 'OM-DTA-002', 'OM-KMG-001', 'OM-STS-001', 'OM-NET-001', 'OM-ADM-001',
    'OV-LGA-001', 'OV-LGA-002',
  ];

  constructor(private api: ApiService) {}

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