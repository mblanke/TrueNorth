import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { MatStepperModule } from '@angular/material/stepper';
import { MatSliderModule } from '@angular/material/slider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatExpansionModule } from '@angular/material/expansion';
import { ApiService, ForgeHistoryItem } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Range as RangeModel } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

interface ForgePreset {
  name: string;
  difficulty: string;
  duration_minutes: number;
  objective_count: number;
  focus_areas: string[];
  description: string;
}

interface ForgeIndicator {
  indicator_id?: string;
  indicator_type: string;
  value: string;
  severity: string;
  mitre_attack_ids: string[];
  description?: string;
}

interface ThreatFeed {
  id: string;
  name: string;
  source_type: string;
  is_active: boolean;
}

interface ForgePreview {
  scenario_yaml: string;
  model_used: string;
  indicators_used: number;
  mitre_techniques: string[];
  estimated_duration_minutes: number;
  objective_count: number;
}

interface ForgeResult {
  exercise_id: string;
  scenario_id: string;
  name: string;
  scenario_yaml: string;
  model_used: string;
  indicators_used: number;
  mitre_techniques: string[];
}

type WizardStep = 'source' | 'configure' | 'preview' | 'result';

@Component({
  selector: 'tn-exercise-forge',
  standalone: true,
  imports: [
    CommonModule, FormsModule, RouterLink, MatCardModule, MatButtonModule, MatIconModule,
    MatFormFieldModule, MatInputModule, MatSelectModule, MatChipsModule,
    MatStepperModule, MatSliderModule, MatProgressBarModule, MatTooltipModule,
    MatDividerModule, MatExpansionModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">auto_fix_high</mat-icon>
          <div>
            <h1>Exercise Forge</h1>
            <p class="subtitle">AI-powered exercise generation from threat intelligence</p>
          </div>
        </div>
      </div>

      <!-- Step Indicators -->
      <div class="step-bar">
        @for (s of steps; track s.key; let i = $index) {
          <div class="step-item"
               [class.active]="currentStep() === s.key"
               [class.completed]="stepIndex(currentStep()) > i"
               [class.clickable]="stepIndex(currentStep()) > i"
               (click)="goToStep(s.key, i)"
               (keyup.enter)="goToStep(s.key, i)"
               tabindex="0"
               role="button">
            <div class="step-circle">
              @if (stepIndex(currentStep()) > i) {
                <mat-icon>check</mat-icon>
              } @else {
                {{ i + 1 }}
              }
            </div>
            <span class="step-label">{{ s.label }}</span>
          </div>
          @if (i < steps.length - 1) {
            <div class="step-connector" [class.active]="stepIndex(currentStep()) > i"></div>
          }
        }
      </div>

      <!-- STEP 1: Source Selection -->
      @if (currentStep() === 'source') {
        <mat-card>
          <mat-card-content>
            <h2>Select Threat Intelligence Source</h2>
            <p class="hint">Choose a threat feed or provide indicators manually.</p>

            <div class="source-toggle">
              <button mat-stroked-button
                      [color]="sourceMode === 'feed' ? 'primary' : ''"
                      (click)="sourceMode = 'feed'">
                <mat-icon>rss_feed</mat-icon> From Feed
              </button>
              <button mat-stroked-button
                      [color]="sourceMode === 'manual' ? 'primary' : ''"
                      (click)="sourceMode = 'manual'">
                <mat-icon>edit_note</mat-icon> Manual Indicators
              </button>
              <button mat-stroked-button
                      [color]="sourceMode === 'curriculum' ? 'primary' : ''"
                      (click)="sourceMode = 'curriculum'">
                <mat-icon>auto_stories</mat-icon> From Curriculum
              </button>
            </div>

            @if (sourceMode === 'curriculum') {
              <div class="mt-2">
                <p class="hint">The exercise is generated to measure the learning objectives below,
                  grounded in the selected curriculum's content. Each objective gets a competency mapping.</p>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Curriculum</mat-label>
                  <mat-select panelClass="tn-select-panel" [(ngModel)]="curriculumId">
                    @for (c of curricula(); track c.id) {
                      <mat-option [value]="c.id" [disabled]="c.status !== 'ready'">
                        {{ c.name }} ({{ c.status }})
                      </mat-option>
                    }
                  </mat-select>
                </mat-form-field>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Learning objectives (one per line)</mat-label>
                  <textarea matInput rows="6" [(ngModel)]="objectivesText"
                            placeholder="Detect a phishing-delivered PowerShell payload&#10;Contain a compromised workstation&#10;Write an incident summary report"></textarea>
                </mat-form-field>
              </div>
            }

            @if (sourceMode === 'feed') {
              <mat-form-field appearance="outline" class="full-width mt-2">
                <mat-label>Threat Intelligence Feed</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="feedId">
                  @for (f of feeds(); track f.id) {
                    <mat-option [value]="f.id">
                      {{ f.name }} ({{ f.source_type }})
                    </mat-option>
                  }
                </mat-select>
              </mat-form-field>
            }

            @if (sourceMode === 'manual') {
              <div class="manual-indicators mt-2">
                @for (ind of manualIndicators; track ind; let i = $index) {
                  <div class="indicator-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Type</mat-label>
                      <mat-select panelClass="tn-select-panel" [(ngModel)]="ind.indicator_type">
                        <mat-option value="ipv4">IPv4</mat-option>
                        <mat-option value="domain">Domain</mat-option>
                        <mat-option value="sha256">SHA-256</mat-option>
                        <mat-option value="url">URL</mat-option>
                        <mat-option value="email">Email</mat-option>
                        <mat-option value="cve">CVE</mat-option>
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="flex-grow">
                      <mat-label>Value</mat-label>
                      <input matInput [(ngModel)]="ind.value" placeholder="e.g. 10.0.0.1 or evil.com">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Severity</mat-label>
                      <mat-select panelClass="tn-select-panel" [(ngModel)]="ind.severity">
                        <mat-option value="low">Low</mat-option>
                        <mat-option value="medium">Medium</mat-option>
                        <mat-option value="high">High</mat-option>
                        <mat-option value="critical">Critical</mat-option>
                      </mat-select>
                    </mat-form-field>
                    <button mat-icon-button color="warn" (click)="removeIndicator(i)" matTooltip="Remove">
                      <mat-icon>delete</mat-icon>
                    </button>
                  </div>
                }
                <button mat-stroked-button (click)="addIndicator()">
                  <mat-icon>add</mat-icon> Add Indicator
                </button>
              </div>
            }

            <div class="step-actions">
              <button mat-raised-button color="primary"
                      (click)="currentStep.set('configure')"
                      [disabled]="!canProceedFromSource()">
                Next <mat-icon>arrow_forward</mat-icon>
              </button>
            </div>
          </mat-card-content>
        </mat-card>
      }

      <!-- STEP 2: Configure -->
      @if (currentStep() === 'configure') {
        <mat-card>
          <mat-card-content>
            <h2>Configure Exercise</h2>

            <!-- Presets -->
            @if (presets().length > 0) {
              <p class="hint">Start from a preset or customise below.</p>
              <div class="presets-grid">
                @for (p of presets(); track p.name) {
                  <div class="preset-card"
                       [class.selected]="selectedPreset === p.name"
                       (click)="applyPreset(p)"
                       (keyup.enter)="applyPreset(p)"
                       tabindex="0"
                       role="button">
                    <strong>{{ p.name }}</strong>
                    <span class="preset-meta">{{ p.difficulty }} · {{ p.duration_minutes }}min · {{ p.objective_count }} objectives</span>
                    <span class="preset-desc">{{ p.description }}</span>
                  </div>
                }
              </div>
              <mat-divider class="my-2"></mat-divider>
            }

            <div class="config-grid">
              <mat-form-field appearance="outline">
                <mat-label>Difficulty</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="config.difficulty">
                  <mat-option value="beginner">Beginner</mat-option>
                  <mat-option value="intermediate">Intermediate</mat-option>
                  <mat-option value="advanced">Advanced</mat-option>
                  <mat-option value="expert">Expert</mat-option>
                </mat-select>
              </mat-form-field>

              <mat-form-field appearance="outline">
                <mat-label>Duration (minutes)</mat-label>
                <input matInput type="number" [(ngModel)]="config.duration_minutes" min="15" max="480">
              </mat-form-field>

              <mat-form-field appearance="outline">
                <mat-label>Objectives Count</mat-label>
                <input matInput type="number" [(ngModel)]="config.objective_count" min="2" max="10">
              </mat-form-field>

              <mat-form-field appearance="outline">
                <mat-label>Range Template</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="config.range_template">
                  <mat-option value="small-enterprise">Small Enterprise</mat-option>
                  <mat-option value="medium-enterprise">Medium Enterprise</mat-option>
                  <mat-option value="large-enterprise">Large Enterprise</mat-option>
                  <mat-option value="cloud-security">Cloud Security</mat-option>
                  <mat-option value="red-team">Red Team</mat-option>
                  <mat-option value="soc-training">SOC Training</mat-option>
                </mat-select>
              </mat-form-field>
            </div>

            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Range (optional)</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="config.range_id">
                <mat-option [value]="null">Tenant default</mat-option>
                @for (r of ranges(); track r.id) {
                  <mat-option [value]="r.id">
                    <span class="range-option">
                      <span>{{ r.name }}</span>
                      <span class="status-chip {{ r.state }}">{{ r.state }}</span>
                    </span>
                  </mat-option>
                }
              </mat-select>
              <mat-hint>Attach the forged exercise to an existing range.</mat-hint>
            </mat-form-field>

            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Focus Areas</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="config.focus_areas" multiple>
                <mat-option value="detection">Detection</mat-option>
                <mat-option value="analysis">Analysis</mat-option>
                <mat-option value="containment">Containment</mat-option>
                <mat-option value="eradication">Eradication</mat-option>
                <mat-option value="recovery">Recovery</mat-option>
              </mat-select>
            </mat-form-field>

            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Exercise Name (optional)</mat-label>
              <input matInput [(ngModel)]="config.name_override" placeholder="Auto-generated if empty">
            </mat-form-field>

            <div class="step-actions">
              <button mat-stroked-button (click)="currentStep.set('source')">
                <mat-icon>arrow_back</mat-icon> Back
              </button>
              <button mat-raised-button color="accent" (click)="doPreview()" [disabled]="previewing()">
                {{ previewing() ? 'Generating preview...' : 'Preview' }}
              </button>
              <button mat-raised-button color="primary" (click)="doGenerate()" [disabled]="generating()">
                {{ generating() ? 'Generating...' : 'Generate Exercise' }}
              </button>
            </div>
          </mat-card-content>
        </mat-card>

        @if (previewing()) {
          <mat-progress-bar mode="indeterminate" class="mt-1"></mat-progress-bar>
        }
      }

      <!-- STEP 3: Preview -->
      @if (currentStep() === 'preview') {
        <mat-card>
          <mat-card-content>
            <h2>Preview Generated Scenario</h2>
            @if (preview()) {
              <div class="preview-meta">
                <mat-chip-set>
                  <mat-chip>Model: {{ preview()!.model_used }}</mat-chip>
                  <mat-chip>Indicators: {{ preview()!.indicators_used }}</mat-chip>
                  <mat-chip>Duration: {{ preview()!.estimated_duration_minutes }}min</mat-chip>
                  <mat-chip>Objectives: {{ preview()!.objective_count }}</mat-chip>
                </mat-chip-set>
              </div>

              @if (preview()!.mitre_techniques.length) {
                <h3>MITRE ATT&CK Techniques</h3>
                <mat-chip-set>
                  @for (t of preview()!.mitre_techniques; track t) {
                    <mat-chip color="accent" highlighted>{{ t }}</mat-chip>
                  }
                </mat-chip-set>
              }

              <h3>Scenario YAML</h3>
              <pre class="yaml-preview">{{ preview()!.scenario_yaml }}</pre>
            }

            <div class="step-actions">
              <button mat-stroked-button (click)="currentStep.set('configure')">
                <mat-icon>arrow_back</mat-icon> Back
              </button>
              <button mat-raised-button color="primary" (click)="doGenerate()" [disabled]="generating()">
                {{ generating() ? 'Creating Exercise...' : 'Accept & Create Exercise' }}
              </button>
            </div>
          </mat-card-content>
        </mat-card>

        @if (generating()) {
          <mat-progress-bar mode="indeterminate" class="mt-1"></mat-progress-bar>
        }
      }

      <!-- STEP 4: Result -->
      @if (currentStep() === 'result') {
        <mat-card>
          <mat-card-content>
            <div class="result-header">
              <mat-icon class="result-icon">check_circle</mat-icon>
              <h2>Exercise Created</h2>
            </div>

            @if (result()) {
              <div class="result-details">
                <div class="detail-row">
                  <span class="label">Name</span>
                  <span>{{ result()!.name }}</span>
                </div>
                <div class="detail-row">
                  <span class="label">Model Used</span>
                  <span>{{ result()!.model_used }}</span>
                </div>
                <div class="detail-row">
                  <span class="label">Indicators Used</span>
                  <span>{{ result()!.indicators_used }}</span>
                </div>
              </div>

              @if (result()!.mitre_techniques.length) {
                <h3>MITRE ATT&CK Coverage</h3>
                <mat-chip-set>
                  @for (t of result()!.mitre_techniques; track t) {
                    <mat-chip color="primary" highlighted>{{ t }}</mat-chip>
                  }
                </mat-chip-set>
              }

              <h3>Generated Scenario</h3>
              <pre class="yaml-preview">{{ result()!.scenario_yaml }}</pre>
            }

            <div class="step-actions">
              @if (result(); as r) {
                <a mat-raised-button color="primary"
                   [routerLink]="['/exercises', r.exercise_id]">
                  <mat-icon>play_circle</mat-icon> Open Exercise
                </a>
                <a mat-stroked-button
                   routerLink="/authoring/scenarios"
                   [queryParams]="{ scenario: r.scenario_id }">
                  <mat-icon>edit_document</mat-icon> Open in Scenario Studio
                </a>
              }
              <button mat-stroked-button (click)="reset()">
                <mat-icon>refresh</mat-icon> Forge Another
              </button>
            </div>
          </mat-card-content>
        </mat-card>
      }

      <!-- Forge history -->
      <mat-accordion class="history-panel mt-3">
        <mat-expansion-panel (opened)="loadHistory()">
          <mat-expansion-panel-header>
            <mat-panel-title>
              <mat-icon class="history-icon">history</mat-icon> Forge History
            </mat-panel-title>
            <mat-panel-description>
              @if (historyTotal() > 0) { {{ historyTotal() }} forged exercises }
            </mat-panel-description>
          </mat-expansion-panel-header>

          @if (historyLoading()) {
            <div class="tn-skeleton-group" aria-busy="true">
              <div class="tn-skeleton tn-skeleton-row"></div>
              <div class="tn-skeleton tn-skeleton-row"></div>
              <div class="tn-skeleton tn-skeleton-row"></div>
            </div>
          } @else if (history().length === 0) {
            <tn-empty-state
              icon="history"
              title="Nothing forged yet"
              message="Generated exercises appear here with their source and MITRE coverage."
            />
          } @else {
            <div class="history-list">
              @for (h of history(); track h.id) {
                <div class="history-row">
                  <div class="history-main">
                    <a class="history-name" [routerLink]="['/exercises', h.exercise_id]">{{ h.exercise_name }}</a>
                    <span class="history-meta">
                      {{ h.created_at ? (h.created_at | date:'medium') : 'unknown date' }}
                      · {{ h.source }} · {{ h.difficulty }} · {{ h.model_used }}
                    </span>
                    @if (h.mitre_techniques.length) {
                      <div class="history-chips">
                        @for (t of h.mitre_techniques; track t) {
                          <span class="status-chip sev-info">{{ t }}</span>
                        }
                      </div>
                    }
                  </div>
                  <a mat-stroked-button
                     routerLink="/authoring/scenarios"
                     [queryParams]="{ scenario: h.scenario_id }">Scenario</a>
                </div>
              }
            </div>
          }
        </mat-expansion-panel>
      </mat-accordion>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .header-left { display: flex; align-items: center; gap: 16px; }
    /* Token note: this file used to reference Material 3 --mat-sys-* variables,
       which the M2 theme never emits — the wizard rendered on transparent
       backgrounds. Everything below is on the app's own token vocabulary. */
    .page-icon { font-size: 36px; width: 36px; height: 36px; color: var(--accent); }
    h1 { margin: 0; }
    .full-width { width: 100%; }
    .mt-1 { margin-top: 8px; }
    .mt-2 { margin-top: 16px; }
    .my-2 { margin: 16px 0; }
    .flex-grow { flex: 1; }
    .hint { color: var(--text-secondary); margin-bottom: 16px; }

    .step-bar { display: flex; align-items: center; margin: 24px 0; gap: 0; }
    .step-item { display: flex; align-items: center; gap: 8px; cursor: default; }
    .step-item.clickable { cursor: pointer; }
    .step-circle {
      width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center;
      justify-content: center; background: var(--bg-surface);
      color: var(--text-secondary); font-weight: 500; font-size: 14px;
      border: 1px solid var(--border);
    }
    .step-item.active .step-circle { background: var(--accent); color: var(--text-on-accent); border-color: var(--accent); }
    .step-item.completed .step-circle { background: var(--success); color: var(--text-on-accent); border-color: var(--success); }
    .step-item.completed .step-circle mat-icon { font-size: 18px; width: 18px; height: 18px; }
    .step-label { font-size: 13px; white-space: nowrap; }
    .step-connector { flex: 1; height: 2px; background: var(--border); margin: 0 8px; min-width: 24px; }
    .step-connector.active { background: var(--success); }

    .source-toggle { display: flex; gap: 12px; }
    .indicator-row { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 4px; }
    .manual-indicators { display: flex; flex-direction: column; gap: 4px; }

    .presets-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
    .preset-card {
      display: flex; flex-direction: column; gap: 4px; padding: 16px; border-radius: var(--radius-md);
      border: 1px solid var(--border); cursor: pointer; transition: all 0.2s;
    }
    .preset-card:hover { border-color: var(--accent); }
    .preset-card.selected { border-color: var(--accent); background: var(--accent-muted); }
    .preset-meta { font-size: 12px; color: var(--text-secondary); }
    .preset-desc { font-size: 13px; }

    .config-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
    .step-actions { display: flex; gap: 12px; margin-top: 24px; }

    .preview-meta { margin-bottom: 16px; }

    .yaml-preview {
      background: var(--bg-surface); color: var(--text-primary);
      border: 1px solid var(--border);
      padding: 16px; border-radius: var(--radius-sm); overflow-x: auto; font-family: var(--font-mono);
      font-size: 13px; line-height: 1.5; max-height: 500px; overflow-y: auto;
      white-space: pre-wrap; word-break: break-word;
    }

    .result-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
    .result-icon { font-size: 40px; width: 40px; height: 40px; color: var(--success); }
    .result-details { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
    .detail-row { display: flex; gap: 12px; }
    .detail-row .label { font-weight: 500; min-width: 140px; color: var(--text-secondary); }

    .range-option { display: inline-flex; align-items: center; gap: 8px; }

    .mt-3 { margin-top: 24px; }
    .history-panel { display: block; }
    .history-icon { margin-right: 8px; vertical-align: middle; color: var(--text-secondary); }
    .history-list { display: flex; flex-direction: column; }
    .history-row {
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 12px 0; border-bottom: 1px solid var(--border);
    }
    .history-row:last-child { border-bottom: none; }
    .history-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .history-name { font-weight: 500; color: var(--accent); text-decoration: none; }
    .history-name:hover { text-decoration: underline; }
    .history-meta { font-size: 12px; color: var(--text-secondary); }
    .history-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  `],
})
export class ExerciseForgeComponent implements OnInit {
  readonly steps: { key: WizardStep; label: string }[] = [
    { key: 'source', label: 'Source' },
    { key: 'configure', label: 'Configure' },
    { key: 'preview', label: 'Preview' },
    { key: 'result', label: 'Result' },
  ];

  currentStep = signal<WizardStep>('source');
  feeds = signal<ThreatFeed[]>([]);
  presets = signal<ForgePreset[]>([]);
  preview = signal<ForgePreview | null>(null);
  result = signal<ForgeResult | null>(null);
  previewing = signal(false);
  generating = signal(false);
  ranges = signal<RangeModel[]>([]);
  history = signal<ForgeHistoryItem[]>([]);
  historyTotal = signal(0);
  historyLoading = signal(false);
  private historyLoaded = false;

  sourceMode: 'feed' | 'manual' | 'curriculum' = 'feed';
  feedId: string | null = null;
  curricula = signal<any[]>([]);
  curriculumId: string | null = null;
  objectivesText = '';
  selectedPreset: string | null = null;
  manualIndicators: ForgeIndicator[] = [
    { indicator_type: 'ipv4', value: '', severity: 'medium', mitre_attack_ids: [] },
  ];

  config = {
    difficulty: 'intermediate',
    duration_minutes: 60,
    objective_count: 4,
    range_template: 'small-enterprise',
    range_id: null as string | null,
    focus_areas: [] as string[],
    name_override: '',
  };

  constructor(
    private api: ApiService,
    private notify: NotificationService,
    private route: ActivatedRoute,
  ) {}

  ngOnInit(): void {
    this.api.get<ThreatFeed[]>('/threat-intel/feeds').subscribe({
      next: f => this.feeds.set(f),
      error: () => this.notify.error('Failed to load threat feeds'),
    });
    this.api.get<ForgePreset[]>('/exercise-forge/presets').subscribe({
      next: p => this.presets.set(p),
      error: () => {},
    });
    this.api.listCurricula().subscribe({
      next: c => this.curricula.set(c),
      error: () => {},
    });
    this.api.listRanges().subscribe({
      next: r => this.ranges.set(r),
      error: () => {},
    });
    // Deep link from Curriculum Forge: /exercise-forge?curriculum=<id>
    const fromCurriculum = this.route.snapshot.queryParamMap.get('curriculum');
    if (fromCurriculum) {
      this.sourceMode = 'curriculum';
      this.curriculumId = fromCurriculum;
    }
  }

  stepIndex(key: WizardStep): number {
    return this.steps.findIndex(s => s.key === key);
  }

  goToStep(key: WizardStep, targetIdx: number): void {
    if (this.stepIndex(this.currentStep()) > targetIdx) {
      this.currentStep.set(key);
    }
  }

  canProceedFromSource(): boolean {
    if (this.sourceMode === 'feed') return !!this.feedId;
    if (this.sourceMode === 'curriculum') {
      return !!this.curriculumId && this.parsedObjectives().length > 0;
    }
    return this.manualIndicators.some(i => i.value.trim().length > 0);
  }

  parsedObjectives(): string[] {
    return this.objectivesText
      .split('\n')
      .map(line => line.trim())
      .filter(Boolean)
      .slice(0, 15);
  }

  addIndicator(): void {
    this.manualIndicators.push({ indicator_type: 'ipv4', value: '', severity: 'medium', mitre_attack_ids: [] });
  }

  removeIndicator(idx: number): void {
    this.manualIndicators.splice(idx, 1);
  }

  applyPreset(p: ForgePreset): void {
    this.selectedPreset = p.name;
    this.config.difficulty = p.difficulty;
    this.config.duration_minutes = p.duration_minutes;
    this.config.objective_count = p.objective_count;
    this.config.focus_areas = [...p.focus_areas];
  }

  private buildPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      difficulty: this.config.difficulty,
      duration_minutes: this.config.duration_minutes,
      objective_count: this.config.objective_count,
      range_template: this.config.range_template,
      focus_areas: this.config.focus_areas,
    };
    if (this.config.name_override?.trim()) {
      payload['name_override'] = this.config.name_override.trim();
    }
    // The backend 404s on a range outside the tenant, so only send a real pick.
    if (this.config.range_id) {
      payload['range_id'] = this.config.range_id;
    }
    if (this.sourceMode === 'feed' && this.feedId) {
      payload['feed_id'] = this.feedId;
    } else if (this.sourceMode === 'curriculum') {
      payload['curriculum_id'] = this.curriculumId;
      payload['learning_objectives'] = this.parsedObjectives();
    } else {
      payload['indicators'] = this.manualIndicators.filter(i => i.value.trim());
    }
    return payload;
  }

  doPreview(): void {
    this.previewing.set(true);
    this.api.post<ForgePreview>('/exercise-forge/preview', this.buildPayload()).subscribe({
      next: p => {
        this.preview.set(p);
        this.previewing.set(false);
        this.currentStep.set('preview');
      },
      error: err => {
        this.previewing.set(false);
        this.notify.error(err?.error?.detail || 'Preview generation failed');
      },
    });
  }

  doGenerate(): void {
    this.generating.set(true);
    this.api.post<ForgeResult>('/exercise-forge/generate', this.buildPayload()).subscribe({
      next: r => {
        this.result.set(r);
        this.generating.set(false);
        this.currentStep.set('result');
        this.notify.success('Exercise forged successfully!');
        this.historyLoaded = false;
        if (this.history().length) this.loadHistory(true);
      },
      error: err => {
        this.generating.set(false);
        this.notify.error(err?.error?.detail || 'Exercise generation failed');
      },
    });
  }

  /** Fetch on first panel open; the panel is collapsed by default so this is lazy. */
  loadHistory(force = false): void {
    if (this.historyLoaded && !force) return;
    this.historyLoaded = true;
    this.historyLoading.set(true);
    this.api.getForgeHistory(25, 0).subscribe({
      next: res => {
        this.history.set(res.items ?? []);
        this.historyTotal.set(res.total ?? 0);
        this.historyLoading.set(false);
      },
      error: () => {
        this.historyLoading.set(false);
        this.historyLoaded = false;
        this.notify.error('Failed to load forge history');
      },
    });
  }

  reset(): void {
    this.currentStep.set('source');
    this.preview.set(null);
    this.result.set(null);
    this.feedId = null;
    this.selectedPreset = null;
    this.manualIndicators = [{ indicator_type: 'ipv4', value: '', severity: 'medium', mitre_attack_ids: [] }];
    this.config = {
      difficulty: 'intermediate',
      duration_minutes: 60,
      objective_count: 4,
      range_template: 'small-enterprise',
      range_id: null,
      focus_areas: [],
      name_override: '',
    };
  }
}
