import { Component, DestroyRef, OnInit, signal, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { interval } from 'rxjs';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiService, RangeOperation, RangeStats } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeSummary, TemplateSummary } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { RangeNotesComponent } from '../../shared/components/range-notes/range-notes.component';
import { CountUpDirective } from '../../shared/motion';

/** States in which the latest operation is still being worked on. */
const IN_PROGRESS = new Set(['provisioning', 'destroying', 'stopping', 'starting']);
/** What each operation is doing while it runs. */
const DOING: Record<string, string> = {
  provision: 'Provisioning', destroy: 'Destroying', stop: 'Powering off', start: 'Powering on',
};
/** How often the list refreshes while any range is in progress. */
export const POLL_MS = 5000;

@Component({
  selector: 'tn-ranges',
  imports: [
    CommonModule, MatCardModule, MatTableModule, MatButtonModule,
    MatIconModule, MatChipsModule, MatDialogModule, MatFormFieldModule,
    MatInputModule, MatSelectModule, MatTooltipModule, FormsModule, RouterLink,
    EmptyStateComponent, CountUpDirective, RangeNotesComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">cloud</mat-icon>
          <div>
            <h1>Ranges</h1>
            <p class="subtitle">Deploy and manage cyber ranges</p>
          </div>
        </div>
        <div class="header-actions">
          <a mat-stroked-button routerLink="/authoring/ranges/designer">
            <mat-icon>architecture</mat-icon> Open Designer
          </a>
          <button mat-raised-button color="primary" (click)="showCreate = !showCreate">
            <mat-icon>add</mat-icon> New Range
          </button>
        </div>
      </div>

      @if (stats(); as s) {
        <div class="stats-strip">
          <div class="stat">
            <span class="stat-value" [tnCountUp]="s.total_ranges"></span>
            <span class="stat-label">Ranges</span>
          </div>
          <div class="stat">
            <span class="stat-value" [tnCountUp]="s.total_vms"></span>
            <span class="stat-label">VMs</span>
          </div>
          <div class="stat">
            <span class="stat-value" [tnCountUp]="s.active_exercises"></span>
            <span class="stat-label">Active exercises</span>
          </div>
          @for (entry of stateEntries(); track entry[0]) {
            <div class="stat">
              <span class="status-chip" [class]="entry[0]">{{ entry[0] }}</span>
              <span class="stat-label">{{ entry[1] }}</span>
            </div>
          }
        </div>
      }

      @if (showCreate) {
        <mat-card class="create-form mt-2">
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Range Name</mat-label>
                <input matInput [(ngModel)]="newName" placeholder="Training Range 01">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Template</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedTemplateId">
                @for (t of templates(); track t.id) {
                  <mat-option [value]="t.id">{{ t.name }} ({{ t.version }})</mat-option>
                }
              </mat-select>
              </mat-form-field>
            </div>
            <button mat-raised-button color="primary" (click)="createRange()" [disabled]="!newName || !selectedTemplateId">
              Create
            </button>
            <button mat-button (click)="showCreate = false">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      @if (editingId) {
        <mat-card class="edit-form mt-2">
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Range Name</mat-label>
                <input matInput [(ngModel)]="editForm.name" placeholder="Range name">
              </mat-form-field>
            </div>
            <button mat-raised-button color="primary" (click)="updateRange()" [disabled]="!editForm.name || saving">
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
            <button mat-button (click)="cancelEdit()" [disabled]="saving">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      <table mat-table [dataSource]="ranges()" class="mt-2 full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let r">{{ r.name }}</td>
        </ng-container>
        <ng-container matColumnDef="state">
          <th mat-header-cell *matHeaderCellDef>State</th>
          <td mat-cell *matCellDef="let r">
            <span class="status-chip" [class]="r.state">{{ r.state }}</span>
            @if (opText(r); as text) {
              <span class="op-status" [class.op-warn]="opWarn(r)" data-testid="op-status">{{ text }}</span>
            }
          </td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let r">{{ r.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let r">
            <a mat-icon-button matTooltip="Open in Designer"
               routerLink="/authoring/ranges/designer" [queryParams]="{ range: r.id }">
              <mat-icon>architecture</mat-icon>
            </a>
            @if (r.state === 'created' || r.state === 'stopped') {
              <button mat-icon-button (click)="startEdit(r)" matTooltip="Rename" [disabled]="saving">
                <mat-icon>edit</mat-icon>
              </button>
            }
            @if (r.state === 'created' || r.state === 'failed') {
              <button mat-icon-button color="primary" (click)="provision(r.id)" [disabled]="busy().has(r.id)"
                      [matTooltip]="r.state === 'failed' ? 'Provision again' : 'Provision'">
                <mat-icon>rocket_launch</mat-icon>
              </button>
            }
            @if (r.state === 'ready' || r.state === 'running') {
              <button mat-icon-button (click)="stop(r.id)" [disabled]="busy().has(r.id)"
                      matTooltip="Power off the range's VMs">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (r.state === 'stopped') {
              <button mat-icon-button color="primary" (click)="start(r.id)" [disabled]="busy().has(r.id)"
                      matTooltip="Power on the range's VMs">
                <mat-icon>play_arrow</mat-icon>
              </button>
            }
            @if (r.state === 'ready' || r.state === 'running' || r.state === 'stopped' || r.state === 'failed') {
              <button mat-icon-button color="warn" (click)="destroy(r.id)" [disabled]="busy().has(r.id)"
                      matTooltip="Destroy">
                <mat-icon>delete</mat-icon>
              </button>
            }
            @if (ops()[r.id]?.error?.['code'] === 'no_outcome') {
              <button mat-stroked-button color="warn" (click)="abandon(r.id)" data-testid="abandon"
                      matTooltip="Only after checking the hypervisor: nothing is still running there">
                Give up on this {{ ops()[r.id].action }}
              </button>
            }
            <button mat-icon-button (click)="toggleNotes(r)"
                    [color]="notesFor()?.id === r.id ? 'primary' : undefined"
                    [matTooltip]="r.description ? 'Description and documents' : 'Add a description'">
              <mat-icon>{{ r.description ? 'description' : 'note_add' }}</mat-icon>
            </button>
            @if (r.error_message) {
              <button mat-icon-button matTooltip="{{ r.error_message }}">
                <mat-icon color="warn">error</mat-icon>
              </button>
            }
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
        <tr mat-row *matRowDef="let row; columns: displayedColumns"></tr>
      </table>

      @if (notesFor(); as sel) {
        <mat-card class="notes-card mt-2">
          <mat-card-content>
            <div class="notes-title">
              <span class="notes-range">{{ sel.name }}</span>
              <button mat-icon-button (click)="notesFor.set(null)" matTooltip="Close">
                <mat-icon>close</mat-icon>
              </button>
            </div>
            <tn-range-notes
              [rangeId]="sel.id"
              [description]="sel.description || ''"
              (descriptionChange)="onDescriptionChanged(sel.id, $event)"
            />
          </mat-card-content>
        </mat-card>
      }

      @if (loading()) {
        <div class="tn-skeleton-group mt-2" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
        </div>
      } @else if (ranges().length === 0) {
        <tn-empty-state
          icon="dns"
          title="No ranges yet"
          message="Create one from a template, then provision it to bring the environment up."
        >
          <button mat-stroked-button (click)="showCreate = true">
            <mat-icon>add</mat-icon> New Range
          </button>
        </tn-empty-state>
      }
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .header-actions { display: flex; gap: 8px; align-items: center; }
    .full-width { width: 100%; }
    .create-form mat-card-content, .edit-form mat-card-content { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
    .create-form mat-form-field, .edit-form mat-form-field { flex: 1; min-width: 200px; }
    .notes-card { border: 1px solid var(--accent); }
    .notes-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .notes-range { font-weight: 600; color: var(--text-primary); }
    .stats-strip {
      display: flex; flex-wrap: wrap; gap: 10px 28px; align-items: center;
      margin: 12px 0 4px; padding: 12px 16px;
      background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    }
    .stat { display: flex; flex-direction: column; gap: 2px; }
    .stat-value { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--text-primary); }
    .stat-label { font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-muted); }
    .op-status { display: block; font-size: 12px; color: var(--text-muted); margin-top: 2px; }
    .op-status.op-warn { color: var(--warn, #b26a00); }
  `],
})
export class RangesComponent implements OnInit {
  private api = inject(ApiService);
  private notify = inject(NotificationService);
  private destroyRef = inject(DestroyRef);

  /** The latest operation of each range that has one in progress. */
  ops = signal<Record<string, RangeOperation>>({});
  /** Ranges with a request on its way, so a double click cannot send two. */
  busy = signal<Set<string>>(new Set());

  ranges = signal<RangeSummary[]>([]);
  templates = signal<TemplateSummary[]>([]);
  stats = signal<RangeStats | null>(null);
  loading = signal(true);
  showCreate = false;
  newName = '';
  selectedTemplateId = '';
  editingId = '';
  editForm = { name: '' };
  saving = false;
  displayedColumns = ['name', 'state', 'created', 'actions'];
  /** The range whose description panel is open, if any. */
  notesFor = signal<RangeSummary | null>(null);

  ngOnInit(): void {
    this.loadRanges();
    // Refresh while anything is in progress: the worker, not this page, decides when it is done.
    interval(POLL_MS).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      if (this.ranges().some(r => IN_PROGRESS.has(r.state))) this.loadRanges();
    });
    this.api.listTemplates().subscribe(t => this.templates.set(t));
  }

  /** Non-zero state counts, for the strip's chips. */
  stateEntries(): [string, number][] {
    const by = this.stats()?.by_state ?? {};
    return Object.entries(by).filter(([, n]) => n > 0);
  }

  loadRanges(): void {
    // Refreshed with the list, so the counts never contradict the rows (the strip used to
    // keep its first-load counts while polling moved ranges on). Stats are decoration: a
    // failure must not blank the page.
    this.api.getRangeStats().subscribe({ next: s => this.stats.set(s), error: () => this.stats.set(null) });
    this.api.listRanges().subscribe({
      next: r => { this.ranges.set(r); this.loading.set(false); this.loadOps(r); },
      error: () => this.loading.set(false),
    });
  }

  /** Latest operation for each range in progress or failed (its message says why). */
  private loadOps(ranges: RangeSummary[]): void {
    const wanted = ranges.filter(r => IN_PROGRESS.has(r.state) || r.state === 'failed');
    const next: Record<string, RangeOperation> = {};
    if (!wanted.length) { this.ops.set(next); return; }
    let left = wanted.length;
    for (const r of wanted) {
      this.api.listRangeOperations(r.id).subscribe({
        next: list => { if (list.length) next[r.id] = list[0]; },
        complete: () => { if (--left === 0) this.ops.set({ ...next }); },
        error: () => { if (--left === 0) this.ops.set({ ...next }); },
      });
    }
  }

  /** What the latest operation is doing, in words; null when there is nothing to add. */
  opText(r: RangeSummary): string | null {
    const op = this.ops()[r.id];
    if (!op) return null;
    const code = String(op.error?.['code'] ?? '');
    const doing = DOING[op.action] ?? op.action;
    if (op.status === 'pending') {
      return code === 'broker_unavailable'
        ? 'Queued: waiting for the task queue to come back'
        : 'Queued';
    }
    if (op.status === 'dispatched') {
      return code === 'no_outcome' ? 'No result from the worker: check the hypervisor' : `${doing}…`;
    }
    if (op.status === 'failed' && r.state === 'failed') {
      return code === 'abandoned' ? `${op.action} abandoned` : `${op.action} failed: ${String(op.error?.['message'] ?? '')}`;
    }
    return null;
  }

  opWarn(r: RangeSummary): boolean {
    const op = this.ops()[r.id];
    const code = String(op?.error?.['code'] ?? '');
    return !!op && (op.status === 'failed' || code === 'broker_unavailable' || code === 'no_outcome');
  }

  private setBusy(id: string, on: boolean): void {
    const next = new Set(this.busy());
    if (on) next.add(id); else next.delete(id);
    this.busy.set(next);
  }

  toggleNotes(r: RangeSummary): void {
    this.notesFor.set(this.notesFor()?.id === r.id ? null : r);
  }

  /** Keep the row in step with an edit made in the panel, without a refetch. */
  onDescriptionChanged(id: string, description: string): void {
    this.ranges.set(this.ranges().map(r => (r.id === id ? { ...r, description } : r)));
    const open = this.notesFor();
    if (open?.id === id) this.notesFor.set({ ...open, description });
  }

  createRange(): void {
    this.api.createRange({ name: this.newName, template_id: this.selectedTemplateId }).subscribe({
      next: () => { this.notify.success('Range created'); this.loadRanges(); this.showCreate = false; this.newName = ''; },
      error: () => this.notify.error('Failed to create range'),
    });
  }

  startEdit(r: RangeSummary): void {
    this.editingId = r.id;
    this.editForm.name = r.name;
  }

  updateRange(): void {
    this.saving = true;
    this.api.updateRange(this.editingId, this.editForm).subscribe({
      next: () => { this.notify.success('Range renamed'); this.loadRanges(); this.cancelEdit(); this.saving = false; },
      error: () => { this.notify.error('Failed to rename range'); this.saving = false; },
    });
  }

  cancelEdit(): void {
    this.editingId = '';
    this.editForm.name = '';
  }

  provision(id: string): void {
    this.request(id, this.api.provisionRange(id), 'Provisioning requested', 'Could not request provisioning');
  }

  stop(id: string): void {
    this.request(id, this.api.stopRange(id), 'Power off requested', 'Could not request power off');
  }

  start(id: string): void {
    this.request(id, this.api.startRange(id), 'Power on requested', 'Could not request power on');
  }

  destroy(id: string): void {
    this.request(id, this.api.destroyRange(id), 'Destroy requested', 'Could not request destroy');
  }

  /** A provision/destroy/stop/start: 202 means recorded, not done; the row's status follows it. */
  private request(id: string, call: ReturnType<ApiService['provisionRange']>, ok: string, failed: string): void {
    this.setBusy(id, true);
    call.subscribe({
      next: () => { this.setBusy(id, false); this.notify.success(ok); this.loadRanges(); },
      error: (e) => {
        this.setBusy(id, false);
        this.notify.error(e?.status === 409 && e?.error?.detail ? e.error.detail : failed);
        this.loadRanges();
      },
    });
  }

  abandon(id: string): void {
    const op = this.ops()[id];
    if (!op) return;
    this.api.abandonRangeOperation(id, op.id).subscribe({
      next: () => { this.notify.success(`${op.action} abandoned; the range is marked failed`); this.loadRanges(); },
      error: () => this.notify.error('Could not abandon the operation'),
    });
  }
}
