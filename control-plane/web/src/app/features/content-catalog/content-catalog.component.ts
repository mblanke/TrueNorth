import {
  ChangeDetectionStrategy, Component, Inject, OnInit, computed, inject, signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import {
  MAT_DIALOG_DATA, MatDialog, MatDialogModule, MatDialogRef,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatTooltipModule } from '@angular/material/tooltip';

import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Template } from '@core/models';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { EnterStaggerDirective, HoverLiftDirective } from '../../shared/motion';

/** What the editor dialog is opened with: null template means "create". */
export interface TemplateEditorData {
  template: Template | null;
}

/** What the editor dialog closes with when the author saves. */
export interface TemplateEditorResult {
  name: string;
  version: string;
  yaml: string;
  is_public: boolean;
}

/** One card's view model: the template plus anything derived from its YAML. */
interface TemplateCard {
  template: Template;
  hosts: number | null;
}

/**
 * Best-effort host count read straight off the YAML text.
 *
 * Templates declare hosts under assets[] (role + optional count) and/or
 * nodes[] (one entry per host) — see api/app/range_topology.py. This counts
 * list items in those two blocks and honours count:, which is enough for a
 * card badge without paying for a round trip per template.
 * Returns null when neither block is present.
 */
export function countTemplateHosts(yaml: string | undefined | null): number | null {
  if (!yaml) {
    return null;
  }
  let inBlock = false;
  let blockIndent = 0;
  let seenBlock = false;
  let hosts = 0;

  for (const line of yaml.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      continue;
    }
    const indent = line.length - line.trimStart().length;

    if (/^(assets|nodes)\s*:/.test(trimmed)) {
      inBlock = true;
      seenBlock = true;
      blockIndent = indent;
      continue;
    }
    if (inBlock && indent <= blockIndent && !trimmed.startsWith('-')) {
      inBlock = false;
    }
    if (!inBlock) {
      continue;
    }
    if (trimmed.startsWith('-')) {
      hosts += 1;
    }
    const body = trimmed.startsWith('-') ? trimmed.slice(1).trim() : trimmed;
    const count = /^count\s*:\s*(\d+)/.exec(body);
    if (count) {
      hosts += Math.max(0, parseInt(count[1], 10) - 1);
    }
  }
  return seenBlock ? hosts : null;
}

/**
 * Create / edit one range template. Validation runs against the real
 * POST /templates/validate so the author sees the engine's own errors
 * before the template is ever saved.
 */
@Component({
  selector: 'tn-template-editor-dialog',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule, MatButtonModule, MatDialogModule, MatFormFieldModule,
    MatIconModule, MatInputModule, MatSlideToggleModule,
  ],
  template: `
    <h2 mat-dialog-title>{{ data.template ? 'Edit Template' : 'New Template' }}</h2>

    <mat-dialog-content>
      <div class="form-row">
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Name</mat-label>
          <input matInput [(ngModel)]="form.name" autocomplete="off">
        </mat-form-field>
        <mat-form-field appearance="outline" class="version">
          <mat-label>Version</mat-label>
          <input matInput [(ngModel)]="form.version" placeholder="1.0" autocomplete="off">
        </mat-form-field>
      </div>

      <mat-slide-toggle [(ngModel)]="form.is_public">
        Public — visible to every tenant
      </mat-slide-toggle>

      <mat-form-field appearance="outline" class="full">
        <mat-label>YAML definition</mat-label>
        <textarea matInput class="yaml" rows="16" spellcheck="false"
                  [(ngModel)]="form.yaml"></textarea>
      </mat-form-field>

      <div class="validate-row">
        <button mat-stroked-button (click)="validate()" [disabled]="!form.yaml || validating()">
          <mat-icon>fact_check</mat-icon> Validate
        </button>
        @if (valid()) {
          <span class="status-chip stable">Valid</span>
        }
        @if (errors().length) {
          <span class="status-chip failed">{{ errors().length }} problem(s)</span>
        }
      </div>

      @if (errors().length) {
        <ul class="errors">
          @for (e of errors(); track $index) {
            <li>
              <span class="path">{{ e.path || 'root' }}</span>
              <span class="dash">—</span>
              <span>{{ e.message }}</span>
            </li>
          }
        </ul>
      }
    </mat-dialog-content>

    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!form.name" (click)="save()">
        {{ data.template ? 'Save Changes' : 'Create' }}
      </button>
    </mat-dialog-actions>
  `,
  styles: [`
    mat-dialog-content { display: block; min-width: min(640px, 80vw); }
    .form-row { display: flex; gap: 16px; align-items: flex-start; }
    .grow { flex: 1; }
    .version { width: 140px; min-width: 140px; }
    .full { width: 100%; margin-top: 12px; }
    .yaml { font-family: var(--font-mono); font-size: 13px; line-height: 1.5; }
    .validate-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .errors {
      list-style: none;
      margin: 0;
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid color-mix(in srgb, var(--severity-high) 40%, transparent);
      background: color-mix(in srgb, var(--severity-high) 10%, transparent);
    }
    .errors li {
      display: flex;
      gap: 6px;
      padding: 3px 0;
      font-size: 0.85rem;
      color: var(--severity-high);
    }
    .errors .path { font-family: var(--font-mono); color: var(--severity-medium); }
    .errors .dash { color: var(--text-muted); }
  `],
})
export class TemplateEditorDialogComponent {
  readonly validating = signal(false);
  readonly valid = signal(false);
  readonly errors = signal<{ path: string; message: string }[]>([]);

  form: TemplateEditorResult;

  private readonly api = inject(ApiService);
  private readonly ref = inject<MatDialogRef<TemplateEditorDialogComponent>>(MatDialogRef);

  constructor(@Inject(MAT_DIALOG_DATA) public data: TemplateEditorData) {
    const t = data?.template;
    this.form = {
      name: t?.name ?? '',
      version: t?.version ?? '1.0',
      yaml: t?.yaml ?? '',
      is_public: t?.is_public ?? false,
    };
  }

  validate(): void {
    this.validating.set(true);
    this.valid.set(false);
    this.api.validateTemplate(this.form.yaml).subscribe({
      next: res => {
        this.validating.set(false);
        this.valid.set(!!res.valid);
        this.errors.set(res.errors ?? []);
      },
      error: () => {
        this.validating.set(false);
        this.errors.set([{ path: '', message: 'Validation request failed.' }]);
      },
    });
  }

  save(): void {
    this.ref.close({ ...this.form });
  }
}

/**
 * Authoring / Content — the range template library.
 *
 * Replaces the old hard-coded catalogue and the standalone Templates page:
 * everything here is a real row of GET /templates.
 */
@Component({
  selector: 'tn-content-catalog',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink, MatButtonModule, MatCardModule,
    MatIconModule, MatTooltipModule, EmptyStateComponent,
    EnterStaggerDirective, HoverLiftDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">library_books</mat-icon>
          <div>
            <h1>Content</h1>
            <p class="subtitle">
              Range templates every range is built from. Author, validate, and reuse them.
            </p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="openEditor(null)">
          <mat-icon>add</mat-icon> New Template
        </button>
      </div>

      @if (loading()) {
        <div class="tn-skeleton-group" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-card"></div>
          <div class="tn-skeleton tn-skeleton-card"></div>
          <div class="tn-skeleton tn-skeleton-card"></div>
        </div>
      } @else {
        <div class="card-grid" tnEnterStagger>
          @for (c of cards(); track c.template.id) {
            <mat-card class="tn-stagger-item template-card" tnHoverLift>
              <mat-card-header>
                <mat-icon mat-card-avatar>description</mat-icon>
                <mat-card-title>{{ c.template.name }}</mat-card-title>
                <mat-card-subtitle>v{{ c.template.version }}</mat-card-subtitle>
              </mat-card-header>

              <mat-card-content>
                <div class="meta">
                  <span class="status-chip"
                        [class.stable]="c.template.is_public"
                        [class.draft]="!c.template.is_public">
                    {{ c.template.is_public ? 'Public' : 'Private' }}
                  </span>
                  @if (c.hosts !== null) {
                    <span class="hosts">
                      <mat-icon aria-hidden="true">dns</mat-icon>
                      {{ c.hosts }} host{{ c.hosts === 1 ? '' : 's' }}
                    </span>
                  }
                  @if (verdict(c.template.id); as v) {
                    <span class="status-chip" [class.stable]="v.valid" [class.failed]="!v.valid">
                      {{ v.valid ? 'Validated' : v.problems + ' problem(s)' }}
                    </span>
                  }
                </div>
              </mat-card-content>

              <mat-card-actions>
                <button mat-button (click)="openEditor(c.template)">
                  <mat-icon>edit</mat-icon> Edit
                </button>
                <button mat-button (click)="validate(c.template)">
                  <mat-icon>fact_check</mat-icon> Validate
                </button>
                <a mat-button color="primary"
                   routerLink="/authoring/ranges/designer"
                   [queryParams]="{ template: c.template.id }">
                  <mat-icon>design_services</mat-icon> Use Template
                </a>
                <button mat-icon-button color="warn" matTooltip="Delete template"
                        aria-label="Delete template" (click)="confirmDelete(c.template)">
                  <mat-icon>delete</mat-icon>
                </button>
              </mat-card-actions>
            </mat-card>
          } @empty {
            <tn-empty-state
              icon="library_books"
              title="No range templates yet"
              message="A template declares the hosts and networks a range is built from. Create one to get started."
            >
              <button mat-stroked-button (click)="openEditor(null)">
                <mat-icon>add</mat-icon> New Template
              </button>
            </tn-empty-state>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .header-left { display: flex; align-items: center; gap: 16px; }
    .page-icon { font-size: 32px; width: 32px; height: 32px; color: var(--accent); }
    h1 { margin: 0; font-size: 24px; }

    .card-grid > tn-empty-state { grid-column: 1 / -1; }

    .template-card {
      border: 1px solid var(--border);
      transition: border-color 0.2s ease;
    }
    .template-card:hover { border-color: var(--accent); }

    .meta { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .hosts {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-family: var(--font-mono);
      font-size: 0.78rem;
      color: var(--text-secondary);
    }
    .hosts mat-icon { font-size: 16px; width: 16px; height: 16px; }

    mat-card-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
  `],
})
export class ContentCatalogComponent implements OnInit {
  readonly templates = signal<Template[]>([]);
  readonly loading = signal(true);
  readonly cards = computed<TemplateCard[]>(() =>
    this.templates().map(t => ({ template: t, hosts: countTemplateHosts(t.yaml) })),
  );

  private readonly verdicts = signal<Record<string, { valid: boolean; problems: number }>>({});

  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.listTemplates().subscribe({
      next: t => {
        this.templates.set(t ?? []);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.notify.error('Could not load templates');
      },
    });
  }

  /** Last validation verdict for a template, or undefined if never validated. */
  verdict(id: string): { valid: boolean; problems: number } | undefined {
    return this.verdicts()[id];
  }

  validate(t: Template): void {
    this.api.validateTemplate(t.yaml).subscribe({
      next: res => {
        const problems = res.errors?.length ?? 0;
        this.verdicts.update(v => ({ ...v, [t.id]: { valid: !!res.valid, problems } }));
        if (res.valid) {
          this.notify.success('Template is valid');
        } else {
          this.notify.error(problems + ' validation problem(s) — open Edit for detail');
        }
      },
      error: () => this.notify.error('Validation failed'),
    });
  }

  openEditor(t: Template | null): void {
    this.dialog
      .open(TemplateEditorDialogComponent, { width: '760px', data: { template: t } })
      .afterClosed()
      .subscribe((result: TemplateEditorResult | undefined) => {
        if (!result?.name) {
          return;
        }
        const call = t
          ? this.api.updateTemplate(t.id, result)
          : this.api.createTemplate(result);
        call.subscribe({
          next: () => {
            this.notify.success(t ? 'Template updated' : 'Template created');
            this.load();
          },
          error: () => this.notify.error(t ? 'Update failed' : 'Create failed'),
        });
      });
  }

  confirmDelete(t: Template): void {
    this.dialog
      .open(ConfirmDialogComponent, {
        data: {
          title: 'Delete Template',
          message: 'Delete "' + t.name + '"? Ranges already built from it are unaffected.',
          confirmText: 'Delete',
        },
      })
      .afterClosed()
      .subscribe(ok => {
        if (ok) {
          this.doDelete(t);
        }
      });
  }

  private doDelete(t: Template): void {
    this.api.deleteTemplate(t.id).subscribe({
      next: () => {
        this.notify.success('Template deleted');
        this.load();
      },
      error: () => this.notify.error('Delete failed'),
    });
  }
}
