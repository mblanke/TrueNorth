import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService, InjectorInfo, YamlValidation } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Scenario, Template } from '@core/models';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { EnterStaggerDirective } from '../../shared/motion';
import {
  COMMON_VALIDATORS,
  OBJECTIVE_TYPES,
  ScenarioModel,
  ObjectiveType,
  emptyScenario,
  fromNormalized,
  normalizeTime,
  slug,
  timeToSeconds,
  toYaml,
  totalPoints,
} from './scenario-yaml.util';

/** Objectives → a drafted scenario, via the orchestrator. */
@Component({
  selector: 'tn-scenario-draft-dialog',
  standalone: true,
  imports: [
    FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule,
    MatInputModule, MatSelectModule,
  ],
  template: `
    <h2 mat-dialog-title>Draft with AI</h2>
    <mat-dialog-content>
      <p class="hint">One objective per line, up to ten. The draft is validated before it
        reaches the editor, so you see any problems immediately.</p>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Training objectives</mat-label>
        <textarea matInput rows="6" name="objectives" [(ngModel)]="objectivesText"
                  placeholder="Detect a phishing-delivered PowerShell payload"></textarea>
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Difficulty</mat-label>
        <mat-select name="difficulty" [(ngModel)]="difficulty">
          <mat-option value="beginner">Beginner</mat-option>
          <mat-option value="intermediate">Intermediate</mat-option>
          <mat-option value="advanced">Advanced</mat-option>
          <mat-option value="expert">Expert</mat-option>
        </mat-select>
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Duration (minutes)</mat-label>
        <input matInput type="number" name="duration" min="15" max="480" [(ngModel)]="durationMinutes" />
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="ref.close()">Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!objectives().length" (click)="submit()">
        Draft scenario
      </button>
    </mat-dialog-actions>
  `,
  styles: [`
    .w { width: 100%; display: block; }
    .hint { color: var(--text-secondary); font-size: 0.86rem; margin: 0 0 12px; }
  `],
})
export class ScenarioDraftDialogComponent {
  objectivesText = '';
  difficulty = 'intermediate';
  durationMinutes = 60;

  constructor(public ref: MatDialogRef<ScenarioDraftDialogComponent>) {}

  objectives(): string[] {
    return this.objectivesText.split('\n').map(o => o.trim()).filter(Boolean).slice(0, 10);
  }

  submit(): void {
    this.ref.close({
      objectives: this.objectives(),
      difficulty: this.difficulty,
      duration_minutes: Number(this.durationMinutes) || 60,
    });
  }
}

/**
 * Scenario Studio — authors the dialect the engine actually runs.
 *
 * Replaces a builder that emitted an invalid shape and could only copy to the
 * clipboard: this one loads and saves real scenarios, validates against the
 * engine's own schema server-side, and offers the registered injectors rather
 * than a hard-coded action list.
 */
@Component({
  selector: 'tn-scenario-studio',
  standalone: true,
  imports: [
    CommonModule, FormsModule, RouterLink, MatButtonModule, MatCardModule, MatChipsModule,
    MatDialogModule, MatFormFieldModule, MatIconModule, MatInputModule,
    MatProgressSpinnerModule, MatSelectModule, MatTooltipModule,
    EmptyStateComponent, EnterStaggerDirective,
  ],
  template: `
    <div class="page-container">
      @if (!editorOpen()) {
        <!-- LIST -->
        <div class="page-header">
          <div class="header-left">
            <mat-icon class="page-icon">theaters</mat-icon>
            <div>
              <h1>Scenarios</h1>
              <p class="subtitle">Author the timeline and objectives an exercise runs on</p>
            </div>
          </div>
          <div class="header-actions">
            <button mat-stroked-button (click)="draftWithAi()">
              <mat-icon>auto_fix_high</mat-icon> Draft with AI
            </button>
            <button mat-raised-button color="primary" (click)="newScenario()">
              <mat-icon>add</mat-icon> New Scenario
            </button>
          </div>
        </div>

        @if (loading()) {
          <div class="tn-skeleton-group mt-2" aria-busy="true">
            <div class="tn-skeleton tn-skeleton-row"></div>
            <div class="tn-skeleton tn-skeleton-row"></div>
            <div class="tn-skeleton tn-skeleton-row"></div>
          </div>
        } @else if (!scenarios().length) {
          <tn-empty-state icon="theaters" title="No scenarios yet"
                          message="Draft one with AI from your objectives, or start from a blank timeline.">
            <button mat-stroked-button (click)="newScenario()">
              <mat-icon>add</mat-icon> New Scenario
            </button>
          </tn-empty-state>
        } @else {
          <div class="list" tnEnterStagger>
            @for (s of scenarios(); track s.id) {
              <mat-card class="row tn-stagger-item">
                <div class="row-main">
                  <span class="row-name">{{ s.name }}</span>
                  <span class="row-meta">v{{ s.version }} · {{ s.updated_at | date:'short' }}</span>
                </div>
                <span class="spacer"></span>
                <button mat-stroked-button (click)="open(s.id)">
                  <mat-icon>edit</mat-icon> Open
                </button>
                <button mat-icon-button color="warn" (click)="confirmDelete(s)" matTooltip="Delete">
                  <mat-icon>delete</mat-icon>
                </button>
              </mat-card>
            }
          </div>
        }
      } @else {
        <!-- EDITOR -->
        <div class="page-header">
          <div class="header-left">
            <button mat-icon-button (click)="closeEditor()" matTooltip="Back to scenarios">
              <mat-icon>arrow_back</mat-icon>
            </button>
            <div>
              <h1>{{ model().name || 'Untitled scenario' }}</h1>
              <p class="subtitle">{{ editingId() ? 'Editing a saved scenario' : 'New scenario' }}</p>
            </div>
          </div>
          <div class="header-actions">
            @if (validation(); as v) {
              <span class="status-chip" [class]="v.valid ? 'stable' : 'failed'">
                {{ v.valid ? 'Valid' : v.errors.length + ' problems' }}
              </span>
            }
            <button mat-stroked-button (click)="validate()" [disabled]="validating()">
              <mat-icon>fact_check</mat-icon> Validate
            </button>
            <button mat-raised-button color="primary" (click)="save()" [disabled]="saving()">
              <mat-icon>save</mat-icon> Save
            </button>
          </div>
        </div>

        @if (validation(); as v) {
          @if (v.errors.length) {
            <mat-card class="problems">
              <mat-card-content>
                <p class="problems-title"><mat-icon>error_outline</mat-icon> Schema problems</p>
                @for (e of v.errors; track e.path + e.message) {
                  <p class="problem"><code>{{ e.path || 'document' }}</code> {{ e.message }}</p>
                }
              </mat-card-content>
            </mat-card>
          }
        }

        <div class="studio">
          <div class="col">
            <mat-card>
              <mat-card-content>
                <h2 class="section-heading">Metadata</h2>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Name</mat-label>
                  <input matInput [ngModel]="model().name" (ngModelChange)="setField('name', $event)" />
                </mat-form-field>
                <div class="two-up">
                  <mat-form-field appearance="outline">
                    <mat-label>Version</mat-label>
                    <input matInput [ngModel]="model().version" (ngModelChange)="setField('version', $event)" />
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Range template</mat-label>
                    <mat-select panelClass="tn-select-panel" [ngModel]="model().range_template"
                                (ngModelChange)="setField('range_template', $event)">
                      @for (t of templates(); track t.id) {
                        <mat-option [value]="t.name">{{ t.name }}</mat-option>
                      }
                      @if (model().range_template && !templateNames().has(model().range_template)) {
                        <mat-option [value]="model().range_template">{{ model().range_template }} (not in catalogue)</mat-option>
                      }
                    </mat-select>
                  </mat-form-field>
                </div>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Description</mat-label>
                  <textarea matInput rows="2" [ngModel]="model().description"
                            (ngModelChange)="setField('description', $event)"></textarea>
                </mat-form-field>
              </mat-card-content>
            </mat-card>

            <mat-card>
              <mat-card-content>
                <div class="section-head">
                  <h2 class="section-heading">Timeline</h2>
                  <span class="count">{{ model().timeline.length }} events</span>
                  <span class="spacer"></span>
                  <button mat-stroked-button (click)="addEvent()">
                    <mat-icon>add</mat-icon> Add event
                  </button>
                </div>

                @if (model().timeline.length) {
                  <!-- Proportional track: where the events actually land in time. -->
                  <div class="track" aria-hidden="true">
                    <div class="track-line"></div>
                    @for (e of model().timeline; track $index) {
                      <span class="tick" [style.left.%]="trackPct(e.t)" [matTooltip]="e.t + ' ' + e.action"></span>
                    }
                  </div>
                }

                @for (e of model().timeline; track i; let i = $index) {
                  <div class="event">
                    <mat-form-field appearance="outline" class="t-field">
                      <mat-label>MM:SS</mat-label>
                      <input matInput [ngModel]="e.t" (ngModelChange)="setEventTime(i, $event)" />
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="grow">
                      <mat-label>Action</mat-label>
                      <mat-select panelClass="tn-select-panel" [ngModel]="e.action"
                                  (ngModelChange)="setEventAction(i, $event)">
                        @for (inj of injectors(); track inj.name) {
                          <mat-option [value]="inj.name">{{ inj.name }}</mat-option>
                        }
                        @if (e.action && !injectorNames().has(e.action)) {
                          <mat-option [value]="e.action">{{ e.action }} (custom)</mat-option>
                        }
                      </mat-select>
                    </mat-form-field>
                    <button mat-icon-button color="warn" (click)="removeEvent(i)" matTooltip="Remove event">
                      <mat-icon>close</mat-icon>
                    </button>

                    @if (injectorFor(e.action); as inj) {
                      <div class="event-detail">
                        @if (inj.description) { <p class="inj-desc">{{ inj.description }}</p> }
                        @if (inj.mitre_techniques.length) {
                          <span class="chips">
                            @for (tech of inj.mitre_techniques; track tech) {
                              <span class="status-chip sev-info">{{ tech }}</span>
                            }
                          </span>
                        }
                        @for (p of inj.required_params; track p) {
                          <mat-form-field appearance="outline" class="param">
                            <mat-label>{{ p }}</mat-label>
                            <input matInput [ngModel]="e.params[p] || ''" (ngModelChange)="setParam(i, p, $event)" />
                          </mat-form-field>
                        }
                      </div>
                    }
                  </div>
                }
              </mat-card-content>
            </mat-card>

            <mat-card>
              <mat-card-content>
                <div class="section-head">
                  <h2 class="section-heading">Objectives</h2>
                  <span class="count" [class.warn]="points() !== 100">{{ points() }} points</span>
                  <span class="spacer"></span>
                  <button mat-stroked-button (click)="addObjective()">
                    <mat-icon>add</mat-icon> Add objective
                  </button>
                </div>

                @for (o of model().objectives; track i; let i = $index) {
                  <div class="objective">
                    <mat-form-field appearance="outline" class="grow">
                      <mat-label>Id</mat-label>
                      <input matInput [ngModel]="o.id" (ngModelChange)="setObjective(i, 'id', $event)" />
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Type</mat-label>
                      <mat-select panelClass="tn-select-panel" [ngModel]="o.type"
                                  (ngModelChange)="setObjective(i, 'type', $event)">
                        @for (t of objectiveTypes; track t) {
                          <mat-option [value]="t">{{ t }}</mat-option>
                        }
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="grow">
                      <mat-label>Validator</mat-label>
                      <mat-select panelClass="tn-select-panel" [ngModel]="o.validator"
                                  (ngModelChange)="setObjective(i, 'validator', $event)">
                        @for (v of validators; track v) {
                          <mat-option [value]="v">{{ v }}</mat-option>
                        }
                        @if (o.validator && !validators.includes(o.validator)) {
                          <mat-option [value]="o.validator">{{ o.validator }} (custom)</mat-option>
                        }
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="pts">
                      <mat-label>Points</mat-label>
                      <input matInput type="number" min="0" [ngModel]="o.points"
                             (ngModelChange)="setObjective(i, 'points', $event)" />
                    </mat-form-field>
                    <button mat-icon-button color="warn" (click)="removeObjective(i)" matTooltip="Remove objective">
                      <mat-icon>close</mat-icon>
                    </button>
                  </div>
                }
              </mat-card-content>
            </mat-card>
          </div>

          <div class="col preview-col">
            <mat-card class="preview-card">
              <mat-card-content>
                <div class="section-head">
                  <h2 class="section-heading">YAML</h2>
                  <span class="spacer"></span>
                  <button mat-icon-button (click)="copyYaml()" matTooltip="Copy YAML">
                    <mat-icon>content_copy</mat-icon>
                  </button>
                </div>
                <pre class="tn-code-block yaml">{{ yaml() }}</pre>
              </mat-card-content>
            </mat-card>
          </div>
        </div>
      }
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .header-actions { display: flex; gap: 8px; align-items: center; }
    .full-width { width: 100%; }
    .spacer { flex: 1 1 auto; }

    .list { display: flex; flex-direction: column; gap: 8px; margin-top: 16px; }
    .row { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
    .row-main { display: flex; flex-direction: column; }
    .row-name { font-weight: 600; color: var(--text-primary); }
    .row-meta { font-size: 0.76rem; color: var(--text-muted); }

    .studio { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 16px; margin-top: 16px; align-items: start; }
    .col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
    .section-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
    .section-heading { margin: 0; font-size: 0.95rem; }
    .count { font-size: 0.76rem; color: var(--text-muted); }
    .count.warn { color: var(--warning); }
    .two-up { display: flex; gap: 12px; flex-wrap: wrap; }
    .two-up mat-form-field { flex: 1; min-width: 180px; }

    .track { position: relative; height: 26px; margin: 4px 0 14px; }
    .track-line { position: absolute; top: 12px; left: 0; right: 0; height: 2px; background: var(--border); border-radius: 1px; }
    .tick {
      position: absolute; top: 6px; width: 12px; height: 12px; margin-left: -6px;
      border-radius: 50%; background: var(--accent); border: 2px solid var(--bg-card);
    }

    .event, .objective { display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }
    .event { border-top: 1px solid var(--border); padding-top: 10px; margin-top: 6px; }
    .t-field { width: 110px; }
    .pts { width: 110px; }
    .grow { flex: 1; min-width: 160px; }
    .event-detail { flex-basis: 100%; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-left: 4px; }
    .inj-desc { flex-basis: 100%; margin: 0 0 4px; font-size: 0.78rem; color: var(--text-secondary); }
    .chips { display: inline-flex; gap: 4px; flex-wrap: wrap; }
    .param { width: 200px; }

    .preview-col { position: sticky; top: 12px; }
    .yaml { margin: 0; max-height: 70vh; overflow: auto; white-space: pre-wrap; word-break: break-word; }

    .problems { margin-top: 12px; border-left: 3px solid var(--alert); }
    .problems-title { display: flex; align-items: center; gap: 6px; margin: 0 0 6px; font-weight: 600; color: var(--alert); }
    .problem { margin: 2px 0; font-size: 0.82rem; color: var(--text-secondary); }
    .problem code { font-family: var(--font-mono); color: var(--text-primary); margin-right: 6px; }

    @media (max-width: 1100px) {
      .studio { grid-template-columns: 1fr; }
      .preview-col { position: static; }
    }
  `],
})
export class ScenarioStudioComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly objectiveTypes = OBJECTIVE_TYPES;
  protected readonly validators = COMMON_VALIDATORS;

  protected readonly scenarios = signal<Scenario[]>([]);
  protected readonly templates = signal<Template[]>([]);
  protected readonly injectors = signal<InjectorInfo[]>([]);
  protected readonly loading = signal(true);
  protected readonly saving = signal(false);
  protected readonly validating = signal(false);
  protected readonly editorOpen = signal(false);
  protected readonly editingId = signal<string | null>(null);
  protected readonly model = signal<ScenarioModel>(emptyScenario());
  protected readonly validation = signal<YamlValidation | null>(null);

  protected readonly yaml = computed(() => toYaml(this.model()));
  protected readonly points = computed(() => totalPoints(this.model()));
  protected readonly injectorNames = computed(() => new Set(this.injectors().map(i => i.name)));
  protected readonly templateNames = computed(() => new Set(this.templates().map(t => t.name)));

  ngOnInit(): void {
    this.loadScenarios();
    this.api.listTemplates().subscribe({ next: t => this.templates.set(t), error: () => this.templates.set([]) });
    // The registry, not a hard-coded list. Failure just means a free-text action.
    this.api.listInjectors().subscribe({ next: i => this.injectors.set(i), error: () => this.injectors.set([]) });

    const deepLink = this.route.snapshot.queryParamMap.get('scenario');
    if (deepLink) this.open(deepLink);
  }

  private loadScenarios(): void {
    this.api.listScenarios().subscribe({
      next: s => { this.scenarios.set(s); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }

  // ── List actions ──────────────────────────────────────────

  protected newScenario(): void {
    this.editingId.set(null);
    this.model.set(emptyScenario());
    this.validation.set(null);
    this.editorOpen.set(true);
  }

  protected open(id: string): void {
    this.api.getScenario(id).subscribe({
      next: sc => {
        this.editingId.set(sc.id);
        // Parse server-side: the validate endpoint hands back the parsed
        // document, so the browser never needs a YAML parser.
        this.api.validateScenario(sc.yaml).subscribe({
          next: v => {
            this.validation.set(v);
            this.model.set(v.normalized ? fromNormalized(v.normalized) : { ...emptyScenario(), name: sc.name });
            this.editorOpen.set(true);
          },
          error: () => {
            this.model.set({ ...emptyScenario(), name: sc.name });
            this.editorOpen.set(true);
          },
        });
      },
      error: () => this.notify.error('Could not open that scenario'),
    });
  }

  protected closeEditor(): void {
    this.editorOpen.set(false);
    this.validation.set(null);
    this.router.navigate([], { relativeTo: this.route, queryParams: {} });
    this.loadScenarios();
  }

  protected confirmDelete(s: Scenario): void {
    this.dialog
      .open(ConfirmDialogComponent, {
        data: { title: 'Delete Scenario', message: `Delete "${s.name}"? This cannot be undone.`, confirmText: 'Delete' },
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.api.deleteScenario(s.id).subscribe({
          next: () => { this.notify.success('Scenario deleted'); this.loadScenarios(); },
          error: () => this.notify.error('Delete failed'),
        });
      });
  }

  // ── Editing ───────────────────────────────────────────────

  protected setField(key: 'name' | 'version' | 'description' | 'range_template', value: string): void {
    this.model.update(m => ({ ...m, [key]: value }));
  }

  protected addEvent(): void {
    const first = this.injectors()[0]?.name ?? '';
    this.model.update(m => ({
      ...m,
      timeline: [...m.timeline, { t: normalizeTime(String(m.timeline.length * 5) + '00'), action: first, params: {} }],
    }));
  }

  protected removeEvent(index: number): void {
    this.model.update(m => ({ ...m, timeline: m.timeline.filter((_, i) => i !== index) }));
  }

  protected setEventTime(index: number, value: string): void {
    this.model.update(m => ({
      ...m,
      timeline: m.timeline.map((e, i) => (i === index ? { ...e, t: normalizeTime(value) } : e)),
    }));
  }

  protected setEventAction(index: number, value: string): void {
    this.model.update(m => ({
      ...m,
      timeline: m.timeline.map((e, i) => (i === index ? { ...e, action: value } : e)),
    }));
  }

  protected setParam(index: number, key: string, value: string): void {
    this.model.update(m => ({
      ...m,
      timeline: m.timeline.map((e, i) => (i === index ? { ...e, params: { ...e.params, [key]: value } } : e)),
    }));
  }

  protected addObjective(): void {
    this.model.update(m => ({
      ...m,
      objectives: [...m.objectives, {
        id: `obj-${m.objectives.length + 1}`,
        type: 'detection' as ObjectiveType,
        validator: COMMON_VALIDATORS[0],
        points: 0,
        params: {},
      }],
    }));
  }

  protected removeObjective(index: number): void {
    this.model.update(m => ({ ...m, objectives: m.objectives.filter((_, i) => i !== index) }));
  }

  protected setObjective(index: number, key: 'id' | 'type' | 'validator' | 'points', value: string): void {
    this.model.update(m => ({
      ...m,
      objectives: m.objectives.map((o, i) => {
        if (i !== index) return o;
        if (key === 'points') return { ...o, points: Number(value) || 0 };
        if (key === 'type') return { ...o, type: value as ObjectiveType };
        return { ...o, [key]: value };
      }),
    }));
  }

  protected injectorFor(action: string): InjectorInfo | null {
    return this.injectors().find(i => i.name === action) ?? null;
  }

  /** Position of an event on the proportional track, 0-100. */
  protected trackPct(t: string): number {
    const max = Math.max(...this.model().timeline.map(e => timeToSeconds(e.t)), 1);
    return Math.min(100, (timeToSeconds(t) / max) * 100);
  }

  // ── Validate / save / draft ───────────────────────────────

  protected validate(): void {
    this.validating.set(true);
    this.api.validateScenario(this.yaml()).subscribe({
      next: v => { this.validation.set(v); this.validating.set(false); },
      error: () => { this.validating.set(false); this.notify.error('Validation service unavailable'); },
    });
  }

  protected save(): void {
    const model = this.model();
    if (!model.name.trim()) {
      this.notify.error('Give the scenario a name first');
      return;
    }
    this.saving.set(true);
    const body = { name: model.name, version: model.version || '1.0', yaml: this.yaml(), is_public: false };
    const id = this.editingId();
    const call = id ? this.api.updateScenario(id, body) : this.api.createScenario(body);
    call.subscribe({
      next: saved => {
        this.editingId.set(saved.id);
        this.saving.set(false);
        this.notify.success(id ? 'Scenario saved' : 'Scenario created');
        this.validate();
      },
      error: () => { this.saving.set(false); this.notify.error('Save failed'); },
    });
  }

  protected draftWithAi(): void {
    this.dialog.open(ScenarioDraftDialogComponent).afterClosed().subscribe(req => {
      if (!req) return;
      this.notify.success('Drafting — this can take a moment');
      this.api.aiScenarioDraft(req).subscribe({
        next: res => {
          // Never paste raw model output into the editor: run it through the
          // validator so it arrives parsed, with its problems already known.
          this.api.validateScenario(res.output || '').subscribe({
            next: v => {
              this.validation.set(v);
              this.model.set(fromNormalized(v.normalized));
              this.editingId.set(null);
              this.editorOpen.set(true);
              if (!this.model().name) this.setField('name', `drafted-${slug(req.objectives[0] ?? 'scenario')}`);
            },
            error: () => this.notify.error('The draft could not be parsed'),
          });
        },
        error: err => this.notify.error(err?.status === 503 ? 'AI orchestrator is unavailable' : 'Drafting failed'),
      });
    });
  }

  protected copyYaml(): void {
    navigator.clipboard?.writeText(this.yaml());
    this.notify.success('YAML copied');
  }
}
