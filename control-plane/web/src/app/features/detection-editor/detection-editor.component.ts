import { Component, signal, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { MatTableModule } from '@angular/material/table';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatDialogModule } from '@angular/material/dialog';
import { MatTooltipModule } from '@angular/material/tooltip';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
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
  detection_yaml: string;
  mitre_attack_ids: string | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

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
  selector: 'tn-detection-editor',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatFormFieldModule, MatInputModule, MatSelectModule, MatChipsModule,
    MatTableModule, MatSnackBarModule, MatDialogModule, MatTooltipModule,
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
            </mat-card-content>
          </mat-card>

          <mat-card class="editor-yaml">
            <mat-card-header>
              <mat-card-title>Sigma YAML</mat-card-title>
              <span class="spacer"></span>
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

              @if (validation()) {
                <div class="validation-result" [class.valid]="validation()!.valid" [class.invalid]="!validation()!.valid">
                  @if (validation()!.valid) {
                    <mat-icon>check_circle</mat-icon> Valid Sigma rule
                  } @else {
                    <mat-icon>error</mat-icon> Validation failed
                  }
                  @for (err of validation()!.errors; track err) {
                    <div class="validation-error">{{ err }}</div>
                  }
                  @for (warn of validation()!.warnings; track warn) {
                    <div class="validation-warning">{{ warn }}</div>
                  }
                </div>
              }
            </mat-card-content>
          </mat-card>

          <div class="editor-actions">
            <button mat-button (click)="cancelEdit()">Cancel</button>
            <button mat-raised-button color="primary" (click)="saveRule()" [disabled]="saving()">
              {{ editingId ? 'Update' : 'Create' }} Rule
            </button>
          </div>
        </div>
      } @else {
        <!-- LIST VIEW -->
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
            title="No detection rules yet"
            message="Create one to get started."
          />
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
    .validation-error, .validation-warning { width: 100%; font-size: 13px; padding-left: 28px; }
    .validation-warning { color: var(--warning); }

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
export class DetectionEditorComponent implements OnInit {
  /** Sigma levels map onto the shared severity chips; only one name differs. */
  levelClass(level: string): string {
    return level === 'informational' ? 'sev-info' : `sev-${level}`;
  }

  rules = signal<DetectionRule[]>([]);
  loading = signal(true);
  editing = signal(false);
  saving = signal(false);
  validation = signal<ValidationResult | null>(null);

  editingId: string | null = null;
  editTitle = '';
  editLevel = 'medium';
  editStatus = 'draft';
  editDescription = '';
  editYaml = '';

  constructor(
    private http: HttpClient,
    private snackBar: MatSnackBar,
  ) {}

  ngOnInit() {
    this.loadRules();
  }

  loadRules() {
    this.http.get<DetectionRule[]>('/api/detection-rules').subscribe({
      next: rules => { this.rules.set(rules); this.loading.set(false); },
      error: () => {
        this.loading.set(false);
        this.snackBar.open('Failed to load rules', 'Close', { duration: 3000 });
      },
    });
  }

  newRule() {
    this.editingId = null;
    this.editTitle = '';
    this.editLevel = 'medium';
    this.editStatus = 'draft';
    this.editDescription = '';
    this.editYaml = SIGMA_TEMPLATE;
    this.validation.set(null);
    this.editing.set(true);
  }

  editRule(rule: DetectionRule) {
    this.editingId = rule.id;
    this.editTitle = rule.title;
    this.editLevel = rule.level;
    this.editStatus = rule.status;
    this.editDescription = rule.description || '';
    this.editYaml = rule.detection_yaml;
    this.validation.set(null);
    this.editing.set(true);
  }

  cancelEdit() {
    this.editing.set(false);
    this.validation.set(null);
  }

  validateYaml() {
    this.http.post<ValidationResult>('/api/detection-rules/validate', { yaml: this.editYaml }).subscribe({
      next: result => this.validation.set(result),
      error: () => this.snackBar.open('Validation request failed', 'Close', { duration: 3000 }),
    });
  }

  saveRule() {
    this.saving.set(true);
    const payload = {
      title: this.editTitle,
      level: this.editLevel,
      status: this.editStatus,
      description: this.editDescription || null,
      detection_yaml: this.editYaml,
    };

    const req = this.editingId
      ? this.http.patch<DetectionRule>(`/api/detection-rules/${this.editingId}`, payload)
      : this.http.post<DetectionRule>('/api/detection-rules', payload);

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
        const msg = err?.error?.detail?.message || 'Failed to save rule';
        this.snackBar.open(msg, 'Close', { duration: 5000 });
        this.saving.set(false);
      },
    });
  }

  deleteRule(rule: DetectionRule) {
    if (!confirm(`Delete rule "${rule.title}"?`)) return;
    this.http.delete(`/api/detection-rules/${rule.id}`).subscribe({
      next: () => {
        this.snackBar.open('Rule deleted', 'Close', { duration: 3000 });
        this.loadRules();
      },
      error: () => this.snackBar.open('Failed to delete rule', 'Close', { duration: 3000 }),
    });
  }
}
