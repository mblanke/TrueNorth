import { Component, OnDestroy, OnInit, computed, effect, inject, input, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { forkJoin, of } from 'rxjs';
import { catchError, map } from 'rxjs/operators';
import {
  NetworkReservation, OPERATION_IN_FLIGHT, RangeDetail, RangeLeaseStatus, RangeOperation, RangeRecoveryApiService,
} from '@core/services/range-recovery-api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  ImpactDialogComponent, ImpactDialogData, ImpactDialogResult,
} from '../../shared/components/impact-dialog/impact-dialog.component';
import { RangeCredentialsComponent } from '../../shared/components/range-credentials/range-credentials.component';
import { RangeTopologyComponent } from '../../shared/components/range-topology/range-topology.component';
import { RangeSnapshotsComponent } from './range-snapshots.component';
import { apiError, rangeCapabilities } from './range-capabilities';

/** A section's load: `data` null while loading (or on error), `error` the API's message. */
interface Loaded<T> { data: T | null; error: string }

const POLL_MS = 10_000;

/**
 * One range's recovery screen (R0-B): its operations, who holds its lease and whether
 * that can be force-released, its network reservations, and its snapshots. Every action
 * states its impact first and shows the API's outcome after; nothing is assumed to have
 * happened until the API says so. While anything is in flight the screen re-reads every
 * 10 s, and says when it last did.
 */
@Component({
  selector: 'tn-range-detail',
  imports: [CommonModule, RouterLink, MatButtonModule, MatCardModule, MatIconModule, MatTooltipModule,
    RangeCredentialsComponent, RangeSnapshotsComponent, RangeTopologyComponent],
  template: `
    <div class="page-container">
      <a mat-button routerLink="/authoring/ranges" class="back"><mat-icon>arrow_back</mat-icon> Ranges</a>

      @if (range().error) {
        <div class="tn-banner error" role="alert">{{ range().error }}</div>
      } @else if (!range().data) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading range"></div>
      } @else {
        @let r = range().data!;
        <header class="page-header">
          <div>
            <h1>{{ r.name }}</h1>
            <p class="subtitle">
              <span class="status-chip" [class]="r.state">{{ r.state }}</span>
              @if (r.provisioner_backend) { <span class="muted">on {{ r.provisioner_backend }}</span> }
            </p>
            @if (r.error_message) {
              <p class="error-text" role="status"><mat-icon inline>error</mat-icon> {{ r.error_message }}</p>
            }
          </div>
          <div class="header-actions">
            <span class="muted" aria-live="polite">
              Read {{ refreshedAt() | date:'mediumTime' }}@if (polling()) { · re-reading every 10 s while work is in flight }
            </span>
            <button mat-stroked-button (click)="reload()" [disabled]="loadingAll()">
              <mat-icon>refresh</mat-icon> Refresh
            </button>
          </div>
        </header>

        @if (outcome(); as o) {
          <div class="tn-banner" [class.error]="o.kind === 'error'" role="status" aria-live="polite">
            <strong>{{ o.title }}</strong>
            @for (line of o.lines; track $index) { <div>{{ line }}</div> }
            <button mat-button (click)="outcome.set(null)">Dismiss</button>
          </div>
        }

        <!-- Topology: the saved design, else drawn from the template and the last build -->
        <mat-card class="section">
          <div class="section-head">
            <h2>Topology</h2>
            <span class="spacer"></span>
            <a mat-stroked-button [routerLink]="['/topology-3d']" [queryParams]="{ range: r.id }">
              <mat-icon>3d_rotation</mat-icon> Open 3D
            </a>
            <a mat-stroked-button [routerLink]="['/authoring/ranges/designer']" [queryParams]="{ range: r.id }">
              <mat-icon>architecture</mat-icon> Open in designer
            </a>
          </div>
          <tn-range-topology [rangeId]="r.id" />
        </mat-card>

        <!-- Operations -->
        <mat-card class="section">
          <h2>Operations</h2>
          <p class="muted">What was asked of this range. Outcomes come from the range's observed state, not from the request.</p>
          @if (operations().error) {
            <p class="error-text" role="alert">{{ operations().error }}</p>
          } @else if (!operations().data) {
            <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
          } @else if (operations().data!.length === 0) {
            <p class="muted">No operations recorded for this range yet.</p>
          } @else {
            <table class="tn-table">
              <caption class="sr-only">Range operations, newest first</caption>
              <thead><tr><th scope="col">#</th><th scope="col">Action</th><th scope="col">Status</th>
                <th scope="col">Accepted</th><th scope="col">Finished</th><th scope="col">Detail</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>
                @for (op of operations().data!; track op.id) {
                  <tr>
                    <td>{{ op.generation }}</td>
                    <td>{{ op.action }}</td>
                    <td><span class="status-chip" [class]="op.status">{{ op.status }}</span></td>
                    <td>{{ op.created_at | date:'short' }}</td>
                    <td>{{ op.finished_at ? (op.finished_at | date:'short') : '—' }}</td>
                    <td class="detail">{{ errorMessage(op) }}</td>
                    <td>
                      @if (inFlight(op) && caps().changeRange) {
                        <button mat-stroked-button color="warn" (click)="abandonOperation(op)"
                                [disabled]="busy()" [attr.aria-label]="'Abandon ' + op.action + ' operation ' + op.generation">
                          Abandon
                        </button>
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          }
        </mat-card>

        <!-- Lease and recovery -->
        <mat-card class="section">
          <h2>Worker lease and recovery</h2>
          @if (lease().error) {
            <p class="error-text" role="alert">{{ lease().error }}</p>
          } @else if (!lease().data) {
            <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
          } @else {
            @let l = lease().data!;
            @if (l.kind === 'none') {
              <p>{{ l.reason }}</p>
            } @else {
              <dl class="facts">
                <dt>Held by</dt><dd><code>{{ l.holder }}</code> ({{ l.kind === 'tombstone' ? 'abandoned operation' : 'running task' }})</dd>
                <dt>Expires</dt><dd>{{ l.expires_at | date:'medium' }} {{ l.blocking ? '(blocks new work except a destroy)' : '(lapsed; no longer blocks)' }}</dd>
                @if (l.in_flight_action) { <dt>Queued behind it</dt><dd>{{ l.in_flight_action }}</dd> }
              </dl>
              <p>{{ l.reason }}</p>
              @if (l.force_release_allowed) {
                @if (caps().forceRelease) {
                  <button mat-stroked-button color="warn" (click)="forceRelease(r, l)" [disabled]="busy()">
                    <mat-icon>lock_open</mat-icon> Force-release lease
                  </button>
                } @else {
                  <p class="muted">An administrator can force-release this lease.</p>
                }
              } @else if (l.guidance) {
                <div class="guidance" role="note">
                  <strong>Force-release is not offered: the API cannot prove nothing is still running.</strong>
                  <p>{{ l.guidance }}</p>
                </div>
              }
            }
          }

          @if (caps().abandonRange) {
            <h3>Abandon the range</h3>
            @if (r.state === 'failed') {
              <p class="muted">For a failed range whose destroy cannot succeed. Platform administrators only.</p>
              <button mat-stroked-button color="warn" (click)="abandonRange(r)" [disabled]="busy()">
                <mat-icon>delete_forever</mat-icon> Abandon range
              </button>
            } @else {
              <p class="muted">Only a failed range can be abandoned; this one is {{ r.state }}.</p>
            }
          }
        </mat-card>

        <!-- Network reservations -->
        <mat-card class="section">
          <h2>Network reservations</h2>
          @if (reservations().error) {
            <p class="error-text" role="alert">{{ reservations().error }}</p>
          } @else if (!reservations().data) {
            <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
          } @else if (reservations().data!.length === 0) {
            <p class="muted">This range holds no addresses or VLANs on shared networks.</p>
          } @else {
            <table class="tn-table">
              <caption class="sr-only">Addresses and VLANs held by this range</caption>
              <thead><tr><th scope="col">Kind</th><th scope="col">Network</th><th scope="col">Value</th><th scope="col">Holder</th></tr></thead>
              <tbody>
                @for (n of reservations().data!; track n.kind + n.domain + n.value) {
                  <tr><td>{{ n.kind }}</td><td>{{ n.domain }}</td><td><code>{{ n.value }}</code></td><td>{{ n.holder }}</td></tr>
                }
              </tbody>
            </table>
          }
        </mat-card>

        <!-- Machine accounts (ADR 0012): masked, fetched on request, audited by the API -->
        <mat-card class="section">
          <tn-range-credentials [rangeId]="r.id" />
        </mat-card>

        <tn-range-snapshots class="section" [rangeId]="r.id" [rangeState]="r.state"
                            [canProvision]="caps().changeRange" [canDelete]="caps().deleteSnapshot"
                            (changed)="reload()" />
      }
    </div>
  `,
  styles: [`
    .back { margin-bottom: 8px; }
    .page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
    .header-actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .section { display: block; margin-top: 16px; padding: 16px; }
    h2 { margin: 0 0 8px; font-size: 18px; }
    .section-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
    .section-head h2 { margin: 0; }
    .spacer { flex: 1; }
    h3 { margin: 16px 0 4px; font-size: 15px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .error-text { color: var(--alert, #ef4444); }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .detail { max-width: 420px; font-size: 13px; }
    .facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0 0 8px; }
    .facts dt { font-weight: 600; color: var(--text-secondary); }
    .guidance { border-left: 3px solid var(--warning, #f59e0b); padding: 8px 12px; background: var(--bg-surface); }
    .tn-banner { border: 1px solid var(--border); border-left: 4px solid var(--accent); padding: 12px; margin-top: 12px; background: var(--bg-card); }
    .tn-banner.error { border-left-color: var(--alert, #ef4444); }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    code { font-family: var(--font-mono); font-size: 12px; }
  `],
})
export class RangeDetailComponent implements OnInit, OnDestroy {
  private api = inject(RangeRecoveryApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);

  /** Bound from the route (`ranges/:id`). */
  readonly id = input.required<string>();

  range = signal<Loaded<RangeDetail>>({ data: null, error: '' });
  operations = signal<Loaded<RangeOperation[]>>({ data: null, error: '' });
  lease = signal<Loaded<RangeLeaseStatus>>({ data: null, error: '' });
  reservations = signal<Loaded<NetworkReservation[]>>({ data: null, error: '' });
  refreshedAt = signal<Date | null>(null);
  loadingAll = signal(false);
  busy = signal(false);
  outcome = signal<{ kind: 'ok' | 'error'; title: string; lines: string[] } | null>(null);

  readonly caps = computed(() => rangeCapabilities(this.auth.user()?.role, this.auth.isPlatformAdmin()));
  readonly polling = computed(() => {
    const ops = this.operations().data ?? [];
    return ops.some(op => this.inFlight(op)) || !!this.lease().data?.blocking;
  });
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    // Re-schedule whenever the in-flight picture changes.
    effect(() => {
      const poll = this.polling();
      this.clearTimer();
      if (poll) this.timer = setTimeout(() => this.reload(), POLL_MS);
    });
  }

  ngOnInit(): void {
    this.reload();
  }

  ngOnDestroy(): void {
    this.clearTimer();
  }

  private clearTimer(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  reload(): void {
    const id = this.id();
    this.loadingAll.set(true);
    const safe = <T>(obs: import('rxjs').Observable<T>) =>
      obs.pipe(map(data => ({ data, error: '' })), catchError(err => of({ data: null, error: apiError(err) })));
    forkJoin({
      range: safe(this.api.getRange(id)),
      operations: safe(this.api.listOperations(id)),
      lease: safe(this.api.getLease(id)),
      reservations: safe(this.api.listNetworkReservations(id)),
    }).subscribe(res => {
      this.range.set(res.range as Loaded<RangeDetail>);
      this.operations.set(res.operations as Loaded<RangeOperation[]>);
      this.lease.set(res.lease as Loaded<RangeLeaseStatus>);
      this.reservations.set(res.reservations as Loaded<NetworkReservation[]>);
      this.refreshedAt.set(new Date());
      this.loadingAll.set(false);
    });
  }

  inFlight(op: RangeOperation): boolean {
    return OPERATION_IN_FLIGHT.includes(op.status);
  }

  errorMessage(op: RangeOperation): string {
    const e = op.error as { message?: string } | null | undefined;
    return e?.message ?? '';
  }

  abandonOperation(op: RangeOperation): void {
    this.confirm({
      title: `Abandon the ${op.action} (operation ${op.generation})?`,
      impact: [
        'Check the hypervisor first: the API cannot see whether this work is still running there.',
        `The operation is recorded as failed and the range goes to "failed", from where it can be destroyed or built again.`,
        'If its worker is still alive it is fenced: it starts nothing more and writes nothing, and keeps the range blocked until its in-flight hypervisor call ends.',
      ],
      confirmText: 'Abandon operation',
      danger: true,
    }, () => this.api.abandonOperation(this.id(), op.id), (out: RangeOperation) => ({
      title: `Operation ${out.generation} is now ${out.status}.`,
      lines: [this.errorMessage(out)].filter(Boolean),
    }));
  }

  forceRelease(r: RangeDetail, l: RangeLeaseStatus): void {
    this.confirm({
      title: 'Force-release the lease?',
      impact: [
        `Deletes ${l.holder}, so the range can be acted on now instead of when it lapses.`,
        `The API's proof: ${l.reason}`,
        "A dead worker's vCenter tasks (clones, port groups, a teardown) can still be running: check vCenter's recent tasks for this range first.",
      ],
      confirmText: 'Force-release',
      danger: true,
      reasonLabel: 'Reason (what you checked)',
      reasonMin: 10,
      typeToConfirm: r.name,
    }, reason => this.api.forceReleaseLease(this.id(), reason), out => ({
      title: `Released ${out.released_holder}${out.was_expired ? ' (it had already lapsed)' : ''}.`,
      lines: [out.warning],
    }));
  }

  abandonRange(r: RangeDetail): void {
    this.confirm({
      title: `Abandon range ${r.name}?`,
      impact: [
        'Records the range destroyed WITHOUT calling the hypervisor.',
        'Whatever is still on a hypervisor stays there: its VMs and port groups must be removed by hand. They are listed on the range and in the audit log.',
        'Its network reservations become reclaimable by other ranges, so any port group still on vCenter must be removed.',
        'Platform administrators only; the API refuses anyone else.',
      ],
      confirmText: 'Abandon range',
      danger: true,
      reasonLabel: 'Reason',
      reasonMin: 10,
      typeToConfirm: r.name,
    }, reason => this.api.abandonRange(this.id(), reason), (out: RangeDetail) => ({
      title: `Range is now ${out.state}.`,
      lines: leftOnHypervisor(out),
    }));
  }

  /** Impact first, then the call, then the API's own outcome; the view re-reads either way. */
  private confirm<T>(
    data: ImpactDialogData,
    call: (reason: string) => import('rxjs').Observable<T>,
    describe: (out: T) => { title: string; lines: string[] },
  ): void {
    this.dialog
      .open<ImpactDialogComponent, ImpactDialogData, ImpactDialogResult | null>(ImpactDialogComponent, {
        width: '560px', data,
      })
      .afterClosed()
      .subscribe(result => {
        if (!result) return;
        this.busy.set(true);
        call(result.reason).subscribe({
          next: out => {
            this.busy.set(false);
            this.outcome.set({ kind: 'ok', ...describe(out) });
            this.reload();
          },
          error: err => {
            this.busy.set(false);
            const msg = apiError(err);
            this.outcome.set({ kind: 'error', title: 'Refused by the API; nothing was changed.', lines: [msg] });
            this.notify.error(msg);
            this.reload();
          },
        });
      });
  }
}

/** What an abandoned range left behind (provisioner_output.abandoned). */
export function leftOnHypervisor(r: RangeDetail): string[] {
  try {
    const out = JSON.parse(r.provisioner_output || '{}');
    const a = out?.abandoned;
    if (!a) return [];
    const vms = (a.vms || []).map((v: { name?: string; vm_id?: string }) => v.name || v.vm_id).filter(Boolean);
    return [
      `Left for manual cleanup: ${vms.length} VM(s)${vms.length ? ': ' + vms.join(', ') : ''}.`,
      `Port groups: ${(a.networks || []).join(', ') || 'none recorded'}.`,
    ];
  } catch {
    return ['The range record could not be read for what was left behind; see the audit log.'];
  }
}
