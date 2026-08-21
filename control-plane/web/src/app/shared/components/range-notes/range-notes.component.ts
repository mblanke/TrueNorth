import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeDocument } from '@core/models';
import { ConfirmDialogComponent } from '../confirm-dialog/confirm-dialog.component';

/**
 * A range's description and its supporting documents.
 *
 * One component for all three places a range gets looked at — the range list,
 * the designer, and the exercise that runs on it — so what a range is for
 * cannot drift between them. Pass readOnly on the viewing surfaces.
 *
 * The description is shown as its source text. No markdown renderer is pulled
 * in just for this (the training views made the same call), so headings and
 * bullets read as written rather than half-rendered.
 */
@Component({
  selector: 'tn-range-notes',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatButtonModule, MatDialogModule,
    MatIconModule, MatTooltipModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="notes">
      <header class="notes-head">
        <h3><mat-icon>description</mat-icon> Range description</h3>
        @if (!readOnly && !editing()) {
          <span class="head-actions">
            <button mat-stroked-button (click)="startEdit()">
              <mat-icon>edit</mat-icon> {{ description ? 'Edit' : 'Add' }}
            </button>
            <button mat-stroked-button (click)="descFile.click()"
                    matTooltip="Replace the description with a text or markdown file">
              <mat-icon>upload_file</mat-icon> Import file
            </button>
          </span>
        }
      </header>

      @if (editing()) {
        <textarea
          class="editor"
          [(ngModel)]="draft"
          rows="14"
          placeholder="What is this range for? How is it meant to be used? Rules of engagement, credentials, gotchas."
        ></textarea>
        <div class="edit-actions">
          <button mat-flat-button color="primary" (click)="save()" [disabled]="saving()">
            <mat-icon>save</mat-icon> {{ saving() ? 'Saving...' : 'Save' }}
          </button>
          <button mat-button (click)="cancelEdit()" [disabled]="saving()">Cancel</button>
        </div>
      } @else if (description) {
        <p class="body">{{ description }}</p>
      } @else {
        <p class="hint">
          No description yet.
          @if (!readOnly) { Add one so the next person knows what this range is for. }
        </p>
      }

      <header class="notes-head docs-head">
        <h3>
          <mat-icon>attach_file</mat-icon> Documents
          @if (documents().length) { <span class="count">{{ documents().length }}</span> }
        </h3>
        @if (!readOnly) {
          <button mat-stroked-button (click)="docFiles.click()">
            <mat-icon>add</mat-icon> Attach
          </button>
        }
      </header>

      @if (loadingDocs()) {
        <div class="tn-skeleton-group" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
        </div>
      } @else if (documents().length) {
        <ul class="doc-list">
          @for (d of documents(); track d.id) {
            <li class="doc">
              <mat-icon class="doc-icon">insert_drive_file</mat-icon>
              <a class="doc-name" [href]="urlFor(d)" target="_blank" rel="noopener"
                 [matTooltip]="d.mime_type || 'file'">{{ d.filename }}</a>
              <span class="doc-size">{{ fmtSize(d.size_bytes) }}</span>
              @if (!readOnly) {
                <button mat-icon-button color="warn" (click)="removeDoc(d)" matTooltip="Remove">
                  <mat-icon>delete</mat-icon>
                </button>
              }
            </li>
          }
        </ul>
      } @else {
        <p class="hint">No documents attached.</p>
      }

      <input #descFile type="file" hidden accept=".md,.markdown,.txt,.text,.rst"
             (change)="onDescriptionFile($event)" />
      <input #docFiles type="file" hidden multiple (change)="onDocumentFiles($event)" />
    </section>
  `,
  styles: [
    `
      :host { display: block; }
      .notes { display: flex; flex-direction: column; gap: 8px; }
      .notes-head {
        display: flex; align-items: center; justify-content: space-between;
        gap: 12px; flex-wrap: wrap;
      }
      .notes-head h3 {
        display: flex; align-items: center; gap: 6px;
        margin: 0; font-size: 0.95rem; color: var(--text-primary);
      }
      .notes-head mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-muted); }
      .head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .docs-head { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
      .count {
        font-size: 0.72rem; padding: 1px 8px; border-radius: 999px;
        background: var(--accent-muted); color: var(--accent);
      }
      /* Source text shown as written: no renderer is pulled in just for this. */
      .body {
        margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
        font-size: 0.86rem; line-height: 1.6; color: var(--text-secondary);
      }
      .hint { margin: 0; font-size: 0.82rem; color: var(--text-muted); }
      .editor {
        width: 100%; box-sizing: border-box; padding: 10px 12px;
        background: var(--bg-input); color: var(--text-primary);
        border: 1px solid var(--border); border-radius: var(--radius-sm);
        font-family: var(--font-mono); font-size: 0.8rem; line-height: 1.6;
        resize: vertical;
      }
      .editor:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
      .edit-actions { display: flex; gap: 8px; }
      .doc-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
      .doc {
        display: flex; align-items: center; gap: 8px;
        padding: 6px 8px; border: 1px solid var(--border);
        border-radius: var(--radius-sm); background: var(--bg-surface);
      }
      .doc-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-muted); }
      .doc-name { flex: 1; min-width: 0; color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
      .doc-name:hover { text-decoration: underline; }
      .doc-size { font-size: 0.74rem; color: var(--text-muted); font-variant-numeric: tabular-nums; }
    `,
  ],
})
export class RangeNotesComponent implements OnChanges {
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);

  @Input({ required: true }) rangeId = '';
  @Input() description = '';
  @Input() readOnly = false;
  /** Emitted after a save or import so the host can keep its own copy in step. */
  @Output() readonly descriptionChange = new EventEmitter<string>();

  protected readonly editing = signal(false);
  protected readonly saving = signal(false);
  protected readonly documents = signal<RangeDocument[]>([]);
  protected readonly loadingDocs = signal(false);
  protected draft = '';

  private loadedFor = '';

  ngOnChanges(): void {
    // Documents belong to a range, so refetch whenever the host points this at a
    // different one — the designer swaps ranges without recreating the component.
    if (this.rangeId && this.rangeId !== this.loadedFor) {
      this.loadedFor = this.rangeId;
      this.loadDocuments();
    }
  }

  protected startEdit(): void {
    this.draft = this.description;
    this.editing.set(true);
  }

  protected cancelEdit(): void {
    this.editing.set(false);
    this.draft = '';
  }

  protected save(): void {
    this.saving.set(true);
    this.api.updateRange(this.rangeId, { description: this.draft }).subscribe({
      next: r => {
        this.description = r.description ?? this.draft;
        this.descriptionChange.emit(this.description);
        this.saving.set(false);
        this.editing.set(false);
        this.notify.success('Description saved');
      },
      error: () => {
        this.saving.set(false);
        this.notify.error('Could not save the description');
      },
    });
  }

  protected onDescriptionFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ''; // so re-picking the same file still fires a change
    if (!file) return;
    this.api.importRangeDescription(this.rangeId, file).subscribe({
      next: r => {
        this.description = r.description ?? '';
        this.descriptionChange.emit(this.description);
        this.notify.success('Description imported from ' + file.name);
      },
      error: err => this.notify.error(err?.error?.detail || 'Import failed'),
    });
  }

  protected onDocumentFiles(event: Event): void {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (!files.length) return;
    this.api.uploadRangeDocuments(this.rangeId, files).subscribe({
      next: created => {
        this.documents.set([...created, ...this.documents()]);
        this.notify.success(
          files.length === 1 ? 'Document attached' : files.length + ' documents attached',
        );
      },
      error: err => this.notify.error(err?.error?.detail || 'Upload failed'),
    });
  }

  protected removeDoc(doc: RangeDocument): void {
    this.dialog
      .open(ConfirmDialogComponent, {
        data: {
          title: 'Remove document',
          message: 'Remove "' + doc.filename + '" from this range? The stored file is deleted.',
          confirmText: 'Remove',
        },
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.api.deleteRangeDocument(this.rangeId, doc.id).subscribe({
          next: () => {
            this.documents.set(this.documents().filter(d => d.id !== doc.id));
            this.notify.success('Document removed');
          },
          error: () => this.notify.error('Could not remove the document'),
        });
      });
  }

  protected urlFor(doc: RangeDocument): string {
    return this.api.rangeDocumentUrl(this.rangeId, doc.id);
  }

  protected fmtSize(bytes: number): string {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  private loadDocuments(): void {
    this.loadingDocs.set(true);
    this.api.listRangeDocuments(this.rangeId).subscribe({
      next: docs => { this.documents.set(docs); this.loadingDocs.set(false); },
      error: () => this.loadingDocs.set(false),
    });
  }
}
