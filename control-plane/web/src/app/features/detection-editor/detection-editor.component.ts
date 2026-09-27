import { Component, signal, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { Subject, debounceTime, distinctUntilChanged, takeUntil } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule, MatChipInputEvent } from '@angular/material/chips';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatTableModule } from '@angular/material/table';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatDialog, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EnterStaggerDirective } from '../../shared/motion';

interface DetectionRule {
  id: string;
  title: string;
  sigma_id: string | null;
  status: string;
  description: string | null;
  author: string | null;
  level: string;
  logsource_category: string | null;
  logsource_product: string | null;
  logsource_service: string | null;
  detection_yaml: string;
  /** The API serialises the list columns to JSON strings on the way out. */
  mitre_attack_ids: string | null;
  false_positives: string | null;
  tags: string | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

interface MitreTechnique {
  id: string;
  name: string;
}

interface AiDraftRequest {
  technique: string;
  data_source: string;
  format: string;
}

/** Same shape the API's DetectionDraftIn enforces server-side. */
const MITRE_ID_PATTERN = /^T\d{4}(\.\d{3})?$/;

const SIGMA_TEMPLATE = `title: My Custom Detection Rule
id: tn-detect-custom-001
status: draft
description: Describe what this rule detects
author: TrueNorth Range
logsource:
  category: process_creation
  product: windows
detection:
  selection:
    Image|endswith: "\\\\suspicious.exe"
  condition: selection
level: medium
falsepositives:
  - Legitimate use of this binary
`;

@Component({
  selector: 'tn-detection-ai-dialog',
  standalone: true,
  imports: [
    FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule,
    MatInputModule, MatSelectModule,
  ],
  template: `
    <h2 mat-dialog-title>Draft with AI</h2>
    <mat-dialog-content>
      <mat-form-field appearance="outline" class="w">
        <mat-label>MITRE technique</mat-label>
        <input matInput name="technique" [(ngModel)]="technique" placeholder="T1059.001" />
        <mat-hint [class.bad]="!!technique && !isValidTechnique()">
          Use an ATT&CK id such as T1059 or T1059.001.
        </mat-hint>
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Data source</mat-label>
        <mat-select name="dataSource" [(ngModel)]="dataSource">
          <mat-option value="sysmon">Sysmon</mat-option>
          <mat-option value="zeek">Zeek</mat-option>
          <mat-option value="suricata">Suricata</mat-option>
        </mat-select>
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Format</mat-label>
        <mat-select name="format" [(ngModel)]="format">
          <mat-option value="sigma">Sigma</mat-option>
          <mat-option value="opensearch">OpenSearch</mat-option>
          <mat-option value="suricata">Suricata</mat-option>
        </mat-select>
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="dialogRef.close()">Cancel</button>
      <button mat-raised-button color="primary"
              [disabled]="!isValidTechnique()" (click)="submit()">Draft</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .w { width: 100%; display: block; }
    mat-hint.bad { color: var(--alert); }
  `],
})
export class DetectionAiDialogComponent {
  technique = '';
  dataSource = 'sysmon';
  format = 'sigma';

  constructor(public dialogRef: MatDialogRef<DetectionAiDialogComponent, AiDraftRequest>) {}

  isValidTechnique(): boolean {
    return MITRE_ID_PATTERN.test(this.technique.trim().toUpperCase());
  }

  submit(): void {
    if (!this.isValidTechnique()) return;
    this.dialogRef.close({
      technique: this.technique.trim().toUpperCase(),
      data_source: this.dataSource,
      format: this.format,
    });
  }
}

@Component({
  selector: 'tn-detection-editor',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatFormFieldModule, MatInputModule, MatSelectModule, MatChipsModule,
    MatCheckboxModule, MatTableModule, MatSnackBarModule, MatDialogModule,
    MatProgressSpinnerModule, MatTooltipModule,
    EmptyStateComponent, EnterStaggerDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">shield</mat-icon>
          <div>
            <h1>Detection Rule Editor</h1>
            <p class="subtitle">Create and manage Sigma-compatible detection rules.</p>
          </div>
        </div>
        <div class="header-actions">
          <button mat-raised-button color="primary" (click)="newRule()">
            <mat-icon>add</mat-icon> New Rule
          </button>
        </div>
      </div>

      @if (editing()) {
        <!-- EDITOR VIEW -->
        <div class="editor-layout">
          <mat-card class="editor-meta">
            <mat-card-content>
              <mat-form-field appearance="outline" class="full-width">
                <mat-label>Title</mat-label>
                <input matInput [(ngModel)]="editTitle" placeholder="Rule title" />
              </mat-form-field>

              <div class="meta-row">
                <mat-form-field appearance="outline">
                  <mat-label>Level</mat-label>
                  <mat-select [(ngModel)]="editLevel">
                    <mat-option value="informational">Informational</mat-option>
                    <mat-option value="low">Low</mat-option>
                    <mat-option value="medium">Medium</mat-option>
                    <mat-option value="high">High</mat-option>
                    <mat-option value="critical">Critical</mat-option>
                  </mat-select>
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Status</mat-label>
                  <mat-select [(ngModel)]="editStatus">
                    <mat-option value="draft">Draft</mat-option>
                    <mat-option value="testing">Testing</mat-option>
                    <mat-option value="stable">Stable</mat-option>
                    <mat-option value="deprecated">Deprecated</mat-option>
                  </mat-select>
                </mat-form-field>
              </div>

              <mat-form-field appearance="outline" class="full-width">
                <mat-label>Description</mat-label>
                <textarea matInput [(ngModel)]="editDescription" rows="2"></textarea>
              </mat-form-field>

              <h3 class="section-heading">Metadata</h3>

              <div class="meta-row">
                <mat-form-field appearance="outline">
                  <mat-label>Sigma ID</mat-label>
                  <input matInput [(ngModel)]="editSigmaId" placeholder="tn-detect-custom-001" />
                </mat-form-field>
                <div class="enabled-toggle">
                  <mat-checkbox [(ngModel)]="editEnabled">Enabled</mat-checkbox>
                </div>
              </div>

              <div class="meta-row">
                <mat-form-field appearance="outline">
                  <mat-label>Logsource category</mat-label>
                  <input matInput [(ngModel)]="editLogsourceCategory" placeholder="process_creation" />
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Logsource product</mat-label>
                  <input matInput [(ngModel)]="editLogsourceProduct" placeholder="windows" />
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Logsource service</mat-label>
                  <input matInput [(ngModel)]="editLogsourceService" placeholder="sysmon" />
                </mat-form-field>
              </div>

              <mat-form-field appearance="outline" class="full-width">
                <mat-label>MITRE ATT&CK techniques</mat-label>
                <mat-chip-grid #mitreGrid aria-label="MITRE technique ids">
                  @for (t of editMitre(); track t) {
                    <mat-chip-row (removed)="removeMitre(t)">
                      {{ t }}
                      <button matChipRemove [attr.aria-label]="'Remove ' + t">
                        <mat-icon>cancel</mat-icon>
                      </button>
                    </mat-chip-row>
                  }
                  <input
                    placeholder="T1059.001"
                    list="tn-mitre-suggestions"
                    [matChipInputFor]="mitreGrid"
                    [matChipInputSeparatorKeyCodes]="separatorKeys"
                    (matChipInputTokenEnd)="addMitre($event)" />
                </mat-chip-grid>
              </mat-form-field>
              @if (mitreError()) {
                <div class="chip-error" role="alert">{{ mitreError() }}</div>
              }
              <datalist id="tn-mitre-suggestions">
                @for (s of suggestions(); track s.id) {
                  <option [value]="s.id">{{ s.name }}</option>
                }
              </datalist>

              <mat-form-field appearance="outline" class="full-width">
                <mat-label>False positives</mat-label>
                <mat-chip-grid #fpGrid aria-label="Known false positives">
                  @for (f of editFalsePositives(); track f) {
                    <mat-chip-row (removed)="removeFalsePositive(f)">
                      {{ f }}
                      <button matChipRemove [attr.aria-label]="'Remove ' + f">
                        <mat-icon>cancel</mat-icon>
                      </button>
                    </mat-chip-row>
                  }
                  <input
                    placeholder="Legitimate admin use"
                    [matChipInputFor]="fpGrid"
                    [matChipInputSeparatorKeyCodes]="separatorKeys"
                    (matChipInputTokenEnd)="addFalsePositive($event)" />
                </mat-chip-grid>
              </mat-form-field>

              <mat-form-field appearance="outline" class="full-width">
                <mat-label>Tags</mat-label>
                <mat-chip-grid #tagGrid aria-label="Rule tags">
                  @for (g of editTags(); track g) {
                    <mat-chip-row (removed)="removeTag(g)">
                      {{ g }}
                      <button matChipRemove [attr.aria-label]="'Remove ' + g">
                        <mat-icon>cancel</mat-icon>
                      </button>
                    </mat-chip-row>
                  }
                  <input
                    placeholder="attack.execution"
                    [matChipInputFor]="tagGrid"
                    [matChipInputSeparatorKeyCodes]="separatorKeys"
                    (matChipInputTokenEnd)="addTag($event)" />
                </mat-chip-grid>
              </mat-form-field>
            </mat-card-content>
          </mat-card>

          <mat-card class="editor-yaml">
            <mat-card-header>
              <mat-card-title>Sigma YAML</mat-card-title>
              <span class="spacer"></span>
              <button mat-stroked-button class="ai-button"
                      [disabled]="aiDrafting()" (click)="openAiDraft()">
                @if (aiDrafting()) {
                  <mat-spinner diameter="16"></mat-spinner> Drafting...
                } @else {
                  <mat-icon>auto_awesome</mat-icon> Draft with AI
                }
              </button>
              <button mat-icon-button matTooltip="Validate YAML" (click)="validateYaml()">
                <mat-icon>check_circle</mat-icon>
              </button>
            </mat-card-header>
            <mat-card-content>
              <textarea
                class="yaml-editor"
                [(ngModel)]="editYaml"
                spellcheck="false"
                placeholder="Paste or write Sigma YAML here..."
              ></textarea>

              @if (validation(); as v) {
                <div class="validation-result" [class.valid]="v.valid" [class.invalid]="!v.valid">
                  @if (v.valid) {
                    <mat-icon>check_circle</mat-icon> Valid Sigma rule
                  } @else {
                    <mat-icon>error</mat-icon> Validation failed
                  }
                </div>
                @if (v.errors.length) {
                  <div class="issue-block errors">
                    @for (err of v.errors; track err) {
                      <div class="issue-line">
                        <mat-icon>error_outline</mat-icon><span>{{ err }}</span>
                      </div>
                    }
                  </div>
                }
                @if (v.warnings.length) {
                  <div class="issue-block warnings">
                    @for (warn of v.warnings; track warn) {
                      <div class="issue-line">
                        <mat-icon>warning_amber</mat-icon><span>{{ warn }}</span>
                      </div>
                    }
                  </div>
                }
              }
            </mat-card-content>
          </mat-card>

          @if (saveErrors().length) {
            <div class="issue-block errors" role="alert">
              @for (msg of saveErrors(); track msg) {
                <div class="issue-line save-error">
                  <mat-icon>error_outline</mat-icon><span>{{ msg }}</span>
                </div>
              }
            </div>
          }

          <div class="editor-actions">
            <button mat-button (click)="cancelEdit()">Cancel</button>
            <button mat-raised-button color="primary" (click)="saveRule()" [disabled]="saving()">
              {{ editingId ? 'Update' : 'Create' }} Rule
            </button>
          </div>
        </div>
      } @else {
        <!-- LIST VIEW -->
        <div class="filter-bar">
          <mat-form-field appearance="outline" class="filter-search">
            <mat-label>Search titles</mat-label>
            <mat-icon matPrefix>search</mat-icon>
            <input matInput [ngModel]="filterSearch"
                   (ngModelChange)="onSearchInput($event)"
                   placeholder="e.g. lsass" />
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Status</mat-label>
            <mat-select [ngModel]="filterStatus" (ngModelChange)="onStatusChange($event)">
              <mat-option value="">All</mat-option>
              <mat-option value="draft">Draft</mat-option>
              <mat-option value="testing">Testing</mat-option>
              <mat-option value="stable">Stable</mat-option>
              <mat-option value="deprecated">Deprecated</mat-option>
            </mat-select>
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Level</mat-label>
            <mat-select [ngModel]="filterLevel" (ngModelChange)="onLevelChange($event)">
              <mat-option value="">All</mat-option>
              <mat-option value="informational">Informational</mat-option>
              <mat-option value="low">Low</mat-option>
              <mat-option value="medium">Medium</mat-option>
              <mat-option value="high">High</mat-option>
              <mat-option value="critical">Critical</mat-option>
            </mat-select>
          </mat-form-field>
        </div>

        <div class="rules-list" tnEnterStagger>
          @for (rule of rules(); track rule.id) {
            <mat-card class="rule-card tn-stagger-item" [class.disabled]="!rule.is_enabled">
              <mat-card-header>
                <mat-icon mat-card-avatar>shield</mat-icon>
                <mat-card-title>{{ rule.title }}</mat-card-title>
                <mat-card-subtitle>
                  <span class="status-chip {{ levelClass(rule.level) }}">{{ rule.level | uppercase }}</span>
                  <span class="status-chip {{ rule.status }}">{{ rule.status }}</span>
                </mat-card-subtitle>
              </mat-card-header>
              <mat-card-content>
                @if (rule.description) {
                  <p>{{ rule.description }}</p>
                }
                <div class="rule-meta">
                  @if (rule.author) { <span><mat-icon>person</mat-icon> {{ rule.author }}</span> }
                  <span><mat-icon>schedule</mat-icon> {{ rule.updated_at | date:'short' }}</span>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-button (click)="editRule(rule)">
                  <mat-icon>edit</mat-icon> Edit
                </button>
                <button mat-button color="warn" (click)="deleteRule(rule)">
                  <mat-icon>delete</mat-icon> Delete
                </button>
              </mat-card-actions>
            </mat-card>
          }
        </div>

        @if (loading()) {
          <div class="tn-skeleton-group mt-2" aria-busy="true">
            <div class="tn-skeleton tn-skeleton-card"></div>
            <div class="tn-skeleton tn-skeleton-card"></div>
            <div class="tn-skeleton tn-skeleton-card"></div>
          </div>
        } @else if (rules().length === 0) {
          <tn-empty-state
            icon="shield"
            [title]="hasFilters() ? 'No rules match these filters' : 'No detection rules yet'"
            [message]="hasFilters() ? 'Clear the filters or widen the search.' : 'Create one to get started.'"
          />
        }

        @if (!loading() && (offset() > 0 || rules().length === pageSize)) {
          <div class="pager">
            <button mat-stroked-button [disabled]="offset() === 0" (click)="prevPage()">
              <mat-icon>chevron_left</mat-icon> Prev
            </button>
            <span class="pager-label">Showing {{ offset() + 1 }}–{{ offset() + rules().length }}</span>
            <button mat-stroked-button [disabled]="rules().length < pageSize" (click)="nextPage()">
              Next <mat-icon>chevron_right</mat-icon>
            </button>
          </div>
        }
      }
    </div>
  `,
  styles: [`
    :host { display: block; padding: 24px; }
    .page-container { max-width: 1200px; margin: 0 auto; }
    .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }
    .header-left { display: flex; align-items: center; gap: 16px; }
    .page-icon { font-size: 32px; width: 32px; height: 32px; color: var(--accent); }
    h1 { margin: 0; font-size: 24px; }

    .editor-layout { display: grid; gap: 16px; }
    .editor-meta .meta-row { display: flex; gap: 16px; }
    .editor-meta .meta-row mat-form-field { flex: 1; }
    .full-width { width: 100%; }

    .yaml-editor {
      width: 100%; min-height: 300px; padding: 12px;
      font-family: var(--font-mono);
      font-size: 13px; line-height: 1.5;
      background: var(--bg-input); color: var(--text-primary);
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      resize: vertical; tab-size: 2;
    }

    .validation-result { margin-top: 8px; padding: 8px 12px; border-radius: var(--radius-sm); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
    .validation-result.valid { background: color-mix(in srgb, var(--success) 14%, transparent); color: var(--success); }
    .validation-result.invalid { background: color-mix(in srgb, var(--alert) 14%, transparent); color: var(--alert); }

    /* Errors and warnings are separate blocks so a valid-but-noisy rule still
       shows its warnings in the warning colour, not the error one. */
    .issue-block {
      margin-top: 8px; padding: 8px 12px; border-radius: var(--radius-sm);
      border-left: 3px solid transparent; display: flex; flex-direction: column; gap: 4px;
    }
    .issue-block.errors {
      background: color-mix(in srgb, var(--alert) 10%, transparent);
      border-left-color: var(--alert); color: var(--alert);
    }
    .issue-block.warnings {
      background: color-mix(in srgb, var(--warning) 10%, transparent);
      border-left-color: var(--warning); color: var(--warning);
    }
    .issue-line { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; }
    .issue-line mat-icon { font-size: 16px; width: 16px; height: 16px; flex-shrink: 0; }

    .section-heading { margin: 20px 0 8px; font-size: 14px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.6px; }
    .enabled-toggle { display: flex; align-items: center; padding-bottom: 22px; }
    .chip-error { font-size: 12px; color: var(--alert); margin: -8px 0 12px; }

    .ai-button { margin-right: 8px; }
    .ai-button mat-spinner { display: inline-block; margin-right: 6px; vertical-align: middle; }

    .filter-bar { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; align-items: flex-start; }
    .filter-bar mat-form-field { min-width: 170px; }
    .filter-search { flex: 1; min-width: 240px; }
    .filter-bar mat-icon[matPrefix] { margin-right: 8px; color: var(--text-muted); }

    .pager { display: flex; align-items: center; justify-content: flex-end; gap: 12px; margin-top: 16px; }
    .pager-label { font-size: 13px; color: var(--text-secondary); }

    .editor-actions { display: flex; gap: 8px; justify-content: flex-end; }
    .spacer { flex: 1; }

    mat-card-header { display: flex; align-items: center; }

    .rules-list { display: grid; gap: 12px; }
    .rule-card.disabled { opacity: 0.6; }
    .rule-meta { display: flex; gap: 16px; font-size: 13px; color: var(--text-secondary); margin-top: 8px; }
    .rule-meta span { display: flex; align-items: center; gap: 4px; }
    .rule-meta mat-icon { font-size: 16px; width: 16px; height: 16px; }

    mat-card-subtitle .status-chip { margin-right: 6px; }
  `],
})
export class DetectionEditorComponent implements OnInit, OnDestroy {
  /** Sigma levels map onto the shared severity chips; only one name differs. */
  levelClass(level: string): string {
    return level === 'informational' ? 'sev-info' : `sev-${level}`;
  }

  readonly pageSize = 20;
  /** ENTER and COMMA — the two keycodes the chip inputs commit on. */
  readonly separatorKeys = [13, 188];

  rules = signal<DetectionRule[]>([]);
  loading = signal(true);
  editing = signal(false);
  saving = signal(false);
  validation = signal<ValidationResult | null>(null);
  saveErrors = signal<string[]>([]);
  suggestions = signal<MitreTechnique[]>([]);
  aiDrafting = signal(false);
  mitreError = signal('');
  offset = signal(0);

  // Server-side filters (see GET /detection-rules query params).
  filterSearch = '';
  filterStatus = '';
  filterLevel = '';

  editingId: string | null = null;
  editTitle = '';
  editLevel = 'medium';
  editStatus = 'draft';
  editDescription = '';
  editYaml = '';

  // The eight persisted fields the editor used to drop on every save.
  editSigmaId = '';
  editLogsourceCategory = '';
  editLogsourceProduct = '';
  editLogsourceService = '';
  editEnabled = true;
  editMitre = signal<string[]>([]);
  editFalsePositives = signal<string[]>([]);
  editTags = signal<string[]>([]);

  private searchInput = new Subject<string>();
  private destroyed = new Subject<void>();

  constructor(
    private api: ApiService,
    private http: HttpClient,
    private dialog: MatDialog,
    private snackBar: MatSnackBar,
  ) {}

  ngOnInit() {
    this.searchInput
      .pipe(debounceTime(300), distinctUntilChanged(), takeUntil(this.destroyed))
      .subscribe(value => {
        this.filterSearch = value;
        this.offset.set(0);
        this.loadRules();
      });
    this.loadSuggestions();
    this.loadRules();
  }

  ngOnDestroy() {
    this.destroyed.next();
    this.destroyed.complete();
  }

  // ── Filters & paging ───────────────────────────────────────
  hasFilters(): boolean {
    return !!(this.filterSearch.trim() || this.filterStatus || this.filterLevel);
  }

  onSearchInput(value: string) {
    this.searchInput.next(value ?? '');
  }

  onStatusChange(value: string) {
    this.filterStatus = value ?? '';
    this.offset.set(0);
    this.loadRules();
  }

  onLevelChange(value: string) {
    this.filterLevel = value ?? '';
    this.offset.set(0);
    this.loadRules();
  }

  prevPage() {
    if (this.offset() === 0) return;
    this.offset.set(Math.max(0, this.offset() - this.pageSize));
    this.loadRules();
  }

  nextPage() {
    if (this.rules().length < this.pageSize) return;
    this.offset.set(this.offset() + this.pageSize);
    this.loadRules();
  }

  /** Every filter and page change round-trips to the server. */
  private listPath(): string {
    const params = new URLSearchParams();
    if (this.filterStatus) params.set('status', this.filterStatus);
    if (this.filterLevel) params.set('level', this.filterLevel);
    const search = this.filterSearch.trim();
    if (search) params.set('search', search);
    params.set('limit', String(this.pageSize));
    params.set('offset', String(this.offset()));
    return `/detection-rules?${params.toString()}`;
  }

  loadRules() {
    this.loading.set(true);
    this.api.get<DetectionRule[]>(this.listPath()).subscribe({
      next: rules => { this.rules.set(rules); this.loading.set(false); },
      error: () => {
        this.loading.set(false);
        this.snackBar.open('Failed to load rules', 'Close', { duration: 3000 });
      },
    });
  }

  private loadSuggestions() {
    // Optional nicety — a missing or malformed asset must not break the editor.
    this.http.get<MitreTechnique[]>('/assets/mitre-common.json').subscribe({
      next: list => this.suggestions.set(Array.isArray(list) ? list : []),
      error: () => this.suggestions.set([]),
    });
  }

  // ── Chip inputs ────────────────────────────────────────────
  addMitre(event: MatChipInputEvent) {
    const raw = (event.value || '').trim().toUpperCase();
    if (!raw) { event.chipInput?.clear(); return; }
    if (!MITRE_ID_PATTERN.test(raw)) {
      this.mitreError.set(`"${raw}" is not an ATT&CK technique id (expected T1059 or T1059.001).`);
      return;
    }
    this.mitreError.set('');
    if (!this.editMitre().includes(raw)) {
      this.editMitre.update(ids => [...ids, raw]);
    }
    event.chipInput?.clear();
  }

  removeMitre(id: string) {
    this.editMitre.update(ids => ids.filter(i => i !== id));
  }

  addFalsePositive(event: MatChipInputEvent) {
    const raw = (event.value || '').trim();
    if (raw && !this.editFalsePositives().includes(raw)) {
      this.editFalsePositives.update(v => [...v, raw]);
    }
    event.chipInput?.clear();
  }

  removeFalsePositive(value: string) {
    this.editFalsePositives.update(v => v.filter(i => i !== value));
  }

  addTag(event: MatChipInputEvent) {
    const raw = (event.value || '').trim();
    if (raw && !this.editTags().includes(raw)) {
      this.editTags.update(v => [...v, raw]);
    }
    event.chipInput?.clear();
  }

  removeTag(value: string) {
    this.editTags.update(v => v.filter(i => i !== value));
  }

  /** List columns come back as JSON strings; tolerate a bare CSV too. */
  private parseList(raw: string | null): string[] {
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch {
      // fall through to the CSV reading below
    }
    return raw.split(',').map(s => s.trim()).filter(Boolean);
  }

  // ── Editor lifecycle ───────────────────────────────────────
  newRule() {
    this.editingId = null;
    this.editTitle = '';
    this.editLevel = 'medium';
    this.editStatus = 'draft';
    this.editDescription = '';
    this.editYaml = SIGMA_TEMPLATE;
    this.editSigmaId = '';
    this.editLogsourceCategory = '';
    this.editLogsourceProduct = '';
    this.editLogsourceService = '';
    this.editEnabled = true;
    this.editMitre.set([]);
    this.editFalsePositives.set([]);
    this.editTags.set([]);
    this.resetEditorFeedback();
    this.editing.set(true);
  }

  editRule(rule: DetectionRule) {
    this.editingId = rule.id;
    this.editTitle = rule.title;
    this.editLevel = rule.level;
    this.editStatus = rule.status;
    this.editDescription = rule.description || '';
    this.editYaml = rule.detection_yaml;
    this.editSigmaId = rule.sigma_id || '';
    this.editLogsourceCategory = rule.logsource_category || '';
    this.editLogsourceProduct = rule.logsource_product || '';
    this.editLogsourceService = rule.logsource_service || '';
    this.editEnabled = rule.is_enabled !== false;
    this.editMitre.set(this.parseList(rule.mitre_attack_ids));
    this.editFalsePositives.set(this.parseList(rule.false_positives));
    this.editTags.set(this.parseList(rule.tags));
    this.resetEditorFeedback();
    this.editing.set(true);
  }

  cancelEdit() {
    this.editing.set(false);
    this.resetEditorFeedback();
  }

  private resetEditorFeedback() {
    this.validation.set(null);
    this.saveErrors.set([]);
    this.mitreError.set('');
  }

  validateYaml() {
    this.api.post<ValidationResult>('/detection-rules/validate', { yaml: this.editYaml }).subscribe({
      next: result => this.validation.set(result),
      error: () => this.snackBar.open('Validation request failed', 'Close', { duration: 3000 }),
    });
  }

  // ── AI drafting ────────────────────────────────────────────
  openAiDraft() {
    const ref = this.dialog.open(DetectionAiDialogComponent, { width: '420px' });
    ref.afterClosed().subscribe((req?: AiDraftRequest) => {
      if (!req) return;
      this.runAiDraft(req);
    });
  }

  private runAiDraft(req: AiDraftRequest) {
    this.aiDrafting.set(true);
    this.api.aiDetectionDraft(req).subscribe({
      next: res => {
        this.aiDrafting.set(false);
        this.editYaml = res.output ?? '';
        if (!this.editMitre().includes(req.technique)) {
          this.editMitre.update(ids => [...ids, req.technique]);
        }
        this.validation.set(null);
        this.snackBar.open(`Draft generated with ${res.model_used}`, 'Close', { duration: 3000 });
      },
      error: () => {
        this.aiDrafting.set(false);
        this.snackBar.open('AI draft failed', 'Close', { duration: 5000 });
      },
    });
  }

  // ── Persistence ────────────────────────────────────────────
  /** The API answers a bad rule with detail: {message, errors[]}. */
  private saveErrorMessages(err: unknown): string[] {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    if (typeof detail === 'string') return [detail];
    if (detail && typeof detail === 'object') {
      const shape = detail as { message?: string; errors?: unknown };
      const messages: string[] = [];
      if (shape.message) messages.push(String(shape.message));
      if (Array.isArray(shape.errors)) messages.push(...shape.errors.map(String));
      if (messages.length) return messages;
    }
    return ['Failed to save rule'];
  }

  saveRule() {
    this.saving.set(true);
    this.saveErrors.set([]);
    const payload = {
      title: this.editTitle,
      level: this.editLevel,
      status: this.editStatus,
      description: this.editDescription || null,
      detection_yaml: this.editYaml,
      sigma_id: this.editSigmaId || null,
      logsource_category: this.editLogsourceCategory || null,
      logsource_product: this.editLogsourceProduct || null,
      logsource_service: this.editLogsourceService || null,
      mitre_attack_ids: this.editMitre(),
      false_positives: this.editFalsePositives(),
      tags: this.editTags(),
      is_enabled: this.editEnabled,
    };

    const req = this.editingId
      ? this.api.patch<DetectionRule>(`/detection-rules/${this.editingId}`, payload)
      : this.api.post<DetectionRule>('/detection-rules', payload);

    req.subscribe({
      next: () => {
        this.snackBar.open(
          `Rule ${this.editingId ? 'updated' : 'created'} successfully`, 'Close', { duration: 3000 }
        );
        this.editing.set(false);
        this.saving.set(false);
        this.loadRules();
      },
      error: err => {
        const messages = this.saveErrorMessages(err);
        this.saveErrors.set(messages);
        this.snackBar.open(messages[0], 'Close', { duration: 5000 });
        this.saving.set(false);
      },
    });
  }

  deleteRule(rule: DetectionRule) {
    const ref = this.dialog.open(ConfirmDialogComponent, {
      data: {
        title: 'Delete detection rule',
        message: `Delete rule "${rule.title}"? This cannot be undone.`,
        confirmText: 'Delete',
      },
    });
    ref.afterClosed().subscribe(confirmed => {
      if (!confirmed) return;
      this.api.delete<void>(`/detection-rules/${rule.id}`).subscribe({
        next: () => {
          this.snackBar.open('Rule deleted', 'Close', { duration: 3000 });
          this.loadRules();
        },
        error: () => this.snackBar.open('Failed to delete rule', 'Close', { duration: 3000 }),
      });
    });
  }
}
