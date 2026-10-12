import { Component, OnChanges, OnDestroy, effect, inject, input, output, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { Observable } from 'rxjs';
import { RangeRecoveryApiService, RangeSnapshot, SNAPSHOT_BUSY } from '@core/services/range-recovery-api.service';
import { NotificationService } from '@core/services/notification.service';
import {
  ImpactDialogComponent, ImpactDialogData, ImpactDialogResult,
} from '../../shared/components/impact-dialog/impact-dialog.component';
import { apiError } from './range-capabilities';

/** Range states a snapshot may be taken in / restored from (routers/ranges.py). */
const SNAPSHOT_FROM = ['ready', 'running', 'stopped'];
const RESTORE_FROM = ['ready', 'running', 'stopped', 'failed'];
const POLL_MS = 10_000;

/**
 * A range's snapshots: list, take, restore, delete. Each is asynchronous on the worker:
 * a snapshot stays "creating" / "restoring" here, re-read every 10 s, until the worker
 * says how it ended. Restore and delete state their impact first.
 */
@Component({
  selector: 'tn-range-snapshots',
  imports: [CommonModule, FormsModule, MatButtonModule, MatCardModule, MatFormFieldModule, MatIconModule,
    MatInputModule],
  template: `
    <mat-card class="panel">
      <h2>Snapshots</h2>
      @if (canProvision()) {
        <form class="create" (ngSubmit)="create()" aria-label="Take a snapshot">
          <mat-form-field appearance="outline">
            <mat-label>Snapshot name</mat-label>
            <input matInput name="snapName" [(ngModel)]="name" maxlength="255" required>
          </mat-form-field>
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Description (optional)</mat-label>
            <input matInput name="snapDesc" [(ngModel)]="description">
          </mat-form-field>
          <button mat-raised-button color="primary" type="submit" [disabled]="!name.trim() || !canSnapshot() || busy()">
            <mat-icon>photo_camera</mat-icon> Take snapshot
          </button>
        </form>
        @if (!canSnapshot()) {
          <p class="muted">A snapshot can be taken only of a ready, running or stopped range; this one is {{ rangeState() }}.</p>
        }
      }

      @if (outcome()) { <p class="outcome" role="status" aria-live="polite">{{ outcome() }}</p> }

      @if (error()) {
        <p class="error-text" role="alert">{{ error() }}</p>
      } @else if (snapshots() === null) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
      } @else if (snapshots()!.length === 0) {
        <p class="muted">No snapshots of this range.</p>
      } @else {
        <table class="tn-table">
          <caption class="sr-only">Snapshots of this range, newest first</caption>
          <thead><tr><th scope="col">Name</th><th scope="col">State</th><th scope="col">Range was</th>
            <th scope="col">Taken</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
          <tbody>
            @for (s of snapshots()!; track s.id) {
              <tr>
                <td>{{ s.name }}@if (s.description) { <div class="muted">{{ s.description }}</div> }</td>
                <td><span class="status-chip" [class]="s.snapshot_state">{{ s.snapshot_state }}</span></td>
                <td>{{ s.range_state_at_snapshot }}</td>
                <td>{{ s.created_at | date:'short' }}</td>
                <td class="actions">
                  @if (canProvision() && s.snapshot_state === 'ready') {
                    <button mat-stroked-button (click)="restore(s)" [disabled]="busy() || !canRestore()"
                            [attr.aria-label]="'Restore snapshot ' + s.name">Restore</button>
                  }
                  @if (canDelete() && !isBusy(s) && s.snapshot_state !== 'deleted') {
                    <button mat-stroked-button color="warn" (click)="remove(s)" [disabled]="busy()"
                            [attr.aria-label]="'Delete snapshot ' + s.name">Delete</button>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      }
    </mat-card>
  `,
  styles: [`
    .panel { padding: 16px; }
    h2 { margin: 0 0 8px; font-size: 18px; }
    .create { display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
    .grow { flex: 1; min-width: 220px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .error-text { color: var(--alert, #ef4444); }
    .outcome { border-left: 4px solid var(--accent); padding: 6px 10px; background: var(--bg-surface); }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .actions { display: flex; gap: 8px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeSnapshotsComponent implements OnChanges, OnDestroy {
  private api = inject(RangeRecoveryApiService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);

  readonly rangeId = input.required<string>();
  readonly rangeState = input<string>('');
  readonly canProvision = input(false);
  readonly canDelete = input(false);
  /** The range itself may have changed (a restore): the parent re-reads it. */
  readonly changed = output<void>();

  snapshots = signal<RangeSnapshot[] | null>(null);
  error = signal('');
  busy = signal(false);
  outcome = signal('');
  name = '';
  description = '';

  readonly canSnapshot = computed(() => SNAPSHOT_FROM.includes(this.rangeState()));
  readonly canRestore = computed(() => RESTORE_FROM.includes(this.rangeState()));
  private readonly anyBusy = computed(() => (this.snapshots() ?? []).some(s => this.isBusy(s)));
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    effect(() => {
      const poll = this.anyBusy();
      this.clearTimer();
      if (poll) this.timer = setTimeout(() => this.load(), POLL_MS);
    });
  }

  ngOnChanges(): void {
    this.load();
  }

  ngOnDestroy(): void {
    this.clearTimer();
  }

  private clearTimer(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  isBusy(s: RangeSnapshot): boolean {
    return SNAPSHOT_BUSY.includes(s.snapshot_state);
  }

  load(): void {
    this.api.listSnapshots(this.rangeId()).subscribe({
      next: list => { this.snapshots.set(list); this.error.set(''); },
      error: err => { this.error.set(apiError(err)); },
    });
  }

  create(): void {
    const name = this.name.trim();
    if (!name) return;
    this.run(this.api.createSnapshot(this.rangeId(), name, this.description.trim()), (s: RangeSnapshot) => {
      this.name = '';
      this.description = '';
      return `Snapshot "${s.name}" accepted: ${s.snapshot_state}. It shows here until the worker reports it ready or failed.`;
    });
  }

  restore(s: RangeSnapshot): void {
    this.ask({
      title: `Restore "${s.name}"?`,
      impact: [
        `Every VM of the range goes back to its state when this snapshot was taken (${s.created_at}).`,
        'Everything since then is lost: files, logs, Student work and any evidence on those VMs.',
        'The range must not be in use: no other operation or restore may be in flight.',
      ],
      confirmText: 'Restore',
      danger: true,
    }, () => this.run(this.api.restoreSnapshot(this.rangeId(), s.id), () => {
      this.changed.emit();
      return `Restore of "${s.name}" accepted; the snapshot shows "restoring" until the worker finishes.`;
    }));
  }

  remove(s: RangeSnapshot): void {
    this.ask({
      title: `Delete "${s.name}"?`,
      impact: [
        'The snapshot is removed from the hypervisor; the range can no longer be restored to it.',
        'The range itself and its VMs are not changed.',
      ],
      confirmText: 'Delete snapshot',
      danger: true,
    }, () => this.run(this.api.deleteSnapshot(this.rangeId(), s.id), () => `Snapshot "${s.name}" deleted.`));
  }

  private ask(data: ImpactDialogData, then: () => void): void {
    this.dialog
      .open<ImpactDialogComponent, ImpactDialogData, ImpactDialogResult | null>(ImpactDialogComponent, {
        width: '520px', data,
      })
      .afterClosed()
      .subscribe(ok => { if (ok) then(); });
  }

  private run<T>(call: Observable<T>, describe: (out: T) => string): void {
    this.busy.set(true);
    this.outcome.set('');
    call.subscribe({
      next: out => { this.busy.set(false); this.outcome.set(describe(out)); this.load(); },
      error: err => {
        this.busy.set(false);
        const msg = apiError(err);
        this.outcome.set(`Refused: ${msg}`);
        this.notify.error(msg);
        this.load();
      },
    });
  }
}
