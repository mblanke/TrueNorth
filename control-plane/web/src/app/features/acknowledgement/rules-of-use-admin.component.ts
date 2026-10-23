import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { AcknowledgementApiService, AcknowledgementVersion } from '@core/services/acknowledgement-api.service';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { MarkdownViewComponent } from '../../shared/markdown/markdown-view.component';

function detail(err: unknown, fallback: string): string {
  const d = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
  return typeof d === 'string' ? d : fallback;
}

/**
 * Administer the rules of use (ADR 0014 §3). The platform administrator edits drafts and
 * publishes them; a published version never changes, so a correction is a new draft.
 * Any admin exports their organisation's signatures as CSV (the platform administrator:
 * every organisation's). The API enforces all of this; the page only hides what would 403.
 */
@Component({
  selector: 'tn-rules-of-use-admin',
  imports: [
    DatePipe,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressBarModule,
    MarkdownViewComponent,
  ],
  template: `
    <section class="page">
      <header>
        <h1>Rules of use</h1>
        <p class="muted">
          Everyone signs the published version before using TrueNorth: on first sign-in, whenever a new
          version is published, and every year.
        </p>
      </header>

      <mat-card class="block">
        <h2>Signatures</h2>
        <p class="muted">
          {{ platformAdmin() ? 'Every organisation’s signatures' : 'Your organisation’s signatures' }}, as a
          spreadsheet (CSV). Each export is audited.
        </p>
        <button mat-stroked-button (click)="exportCsv()" [disabled]="exporting()">
          <mat-icon>download</mat-icon> Export signatures
        </button>
      </mat-card>

      @if (platformAdmin()) {
        <mat-card class="block">
          <div class="row">
            <h2>Versions</h2>
            <button mat-stroked-button (click)="newDraft()" [disabled]="busy()">
              <mat-icon>add</mat-icon> New draft
            </button>
          </div>
          @if (loading()) {
            <mat-progress-bar mode="indeterminate" />
          }
          <ul class="versions">
            @for (v of versions(); track v.id) {
              <li [class.selected]="selected()?.id === v.id">
                <button mat-button (click)="select(v)">
                  @if (v.version) {
                    Version {{ v.version }} · published {{ v.published_at | date: 'mediumDate' }}
                    @if (v.id === currentId()) { <strong>(current)</strong> }
                  } @else {
                    Draft · last changed {{ (v.updated_at || v.created_at) | date: 'medium' }}
                  }
                </button>
              </li>
            } @empty {
              <li class="muted">No versions yet. Start a draft.</li>
            }
          </ul>
        </mat-card>

        @if (selected(); as v) {
          <mat-card class="block">
            @if (v.version) {
              <h2>Version {{ v.version }} (published, read-only)</h2>
              <p class="muted">A published version never changes. To correct it, start a new draft from it.</p>
              <div class="preview"><tn-markdown-view [source]="v.text" /></div>
            } @else {
              <h2>Draft</h2>
              <div class="editor">
                <mat-form-field appearance="outline" class="text">
                  <mat-label>Text (Markdown)</mat-label>
                  <textarea matInput rows="22" [(ngModel)]="draftText" (ngModelChange)="dirty.set(true)"></textarea>
                </mat-form-field>
                <div class="preview" aria-label="Preview">
                  <tn-markdown-view [source]="draftText" />
                </div>
              </div>
              <div class="row">
                <button mat-stroked-button (click)="save(v)" [disabled]="busy() || !dirty() || !draftText.trim()">
                  Save draft
                </button>
                <button mat-flat-button color="primary" (click)="publish(v)" [disabled]="busy() || !draftText.trim()">
                  Publish
                </button>
              </div>
            }
          </mat-card>
        }
      } @else {
        <p class="muted">Only the platform administrator edits and publishes the rules of use.</p>
      }
    </section>
  `,
  styles: [
    `
      .page {
        display: flex;
        flex-direction: column;
        gap: 1rem;
        padding: 1rem;
        max-width: 1200px;
      }
      h1 {
        margin: 0 0 0.25rem;
      }
      h2 {
        margin: 0 0 0.5rem;
        font-size: 1.125rem;
      }
      .muted {
        opacity: 0.7;
        margin: 0 0 0.5rem;
      }
      .block {
        padding: 1rem;
      }
      .row {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
      }
      .versions {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .versions li.selected {
        background: var(--mat-sys-secondary-container, rgba(127, 127, 127, 0.12));
        border-radius: 8px;
      }
      .editor {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1rem;
      }
      @media (max-width: 900px) {
        .editor {
          grid-template-columns: 1fr;
        }
      }
      .text {
        width: 100%;
      }
      .preview {
        max-height: 32rem;
        overflow: auto;
        padding: 0 1rem;
        border: 1px solid var(--mat-sys-outline-variant, rgba(127, 127, 127, 0.35));
        border-radius: 8px;
      }
    `,
  ],
})
export class RulesOfUseAdminComponent implements OnInit {
  private readonly api = inject(AcknowledgementApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);
  private readonly router = inject(Router);

  readonly platformAdmin = this.auth.isPlatformAdmin;
  readonly versions = signal<AcknowledgementVersion[]>([]);
  readonly selected = signal<AcknowledgementVersion | null>(null);
  readonly loading = signal(false);
  readonly busy = signal(false);
  readonly exporting = signal(false);
  readonly dirty = signal(false);
  draftText = '';

  /** The highest published version: what everyone signs now. */
  readonly currentId = computed(() => {
    const published = this.versions().filter(v => v.version != null);
    return published.length ? published.reduce((a, b) => ((a.version ?? 0) > (b.version ?? 0) ? a : b)).id : null;
  });

  ngOnInit(): void {
    if (this.platformAdmin()) this.load();
  }

  load(selectId?: string): void {
    this.loading.set(true);
    this.api.versions().subscribe({
      next: rows => {
        this.loading.set(false);
        this.versions.set(rows);
        const pick = rows.find(r => r.id === selectId) ?? rows.find(r => r.version == null) ?? rows[0] ?? null;
        if (pick) this.select(pick);
      },
      error: err => {
        this.loading.set(false);
        this.notify.error(detail(err, 'Could not load the versions'));
      },
    });
  }

  select(v: AcknowledgementVersion): void {
    this.selected.set(v);
    this.draftText = v.text;
    this.dirty.set(false);
  }

  /** A new draft starts from the current version's text, or the selected draft's. */
  newDraft(): void {
    const base = this.versions().find(v => v.id === this.currentId())?.text ?? this.selected()?.text ?? '# Rules of use\n';
    this.busy.set(true);
    this.api.createDraft(base).subscribe({
      next: row => {
        this.busy.set(false);
        this.load(row.id);
      },
      error: err => {
        this.busy.set(false);
        this.notify.error(detail(err, 'Could not start a draft'));
      },
    });
  }

  save(v: AcknowledgementVersion): void {
    this.busy.set(true);
    this.api.updateDraft(v.id, this.draftText).subscribe({
      next: row => {
        this.busy.set(false);
        this.notify.success('Draft saved');
        this.load(row.id);
      },
      error: err => {
        this.busy.set(false);
        this.notify.error(detail(err, 'Could not save the draft'));
      },
    });
  }

  publish(v: AcknowledgementVersion): void {
    this.dialog
      .open(ConfirmDialogComponent, {
        data: {
          title: 'Publish the rules of use',
          message:
            'Everyone, you included, will have to read and agree to this version before doing anything else. ' +
            'A published version cannot be changed.',
          confirmText: 'Publish',
        },
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.busy.set(true);
        const text = this.draftText;
        const save$ = this.dirty() ? this.api.updateDraft(v.id, text) : null;
        const doPublish = () =>
          this.api.publish(v.id).subscribe({
            next: () => {
              this.busy.set(false);
              this.notify.success('Published. Everyone signs it next time they use TrueNorth.');
              // The API now refuses the publisher too until they sign: go to the form, then back.
              this.auth.setAcknowledgementRequired(true);
              void this.router.navigate(['/rules-of-use'], { queryParams: { returnUrl: '/admin/rules-of-use' } });
            },
            error: err => {
              this.busy.set(false);
              this.notify.error(detail(err, 'Could not publish'));
            },
          });
        if (save$) {
          save$.subscribe({
            next: () => doPublish(),
            error: err => {
              this.busy.set(false);
              this.notify.error(detail(err, 'Could not save the draft'));
            },
          });
        } else {
          doPublish();
        }
      });
  }

  exportCsv(): void {
    this.exporting.set(true);
    this.api.exportSignatures().subscribe({
      next: blob => {
        this.exporting.set(false);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `acknowledgement-signatures-${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: err => {
        this.exporting.set(false);
        this.notify.error(detail(err, 'Could not export the signatures'));
      },
    });
  }
}
