import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { roleCan } from '@core/auth/permissions';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  DriftFinding, InventoryItem, RangeInventoryApiService, RangeInventoryChecks,
} from '@core/services/range-inventory-api.service';

const STATUS_LABEL: Record<string, string> = {
  pass: 'Pass', fail: 'Fail', incomplete: 'Incomplete', error: 'Not checked',
};
const FINDING_LABEL: Record<string, string> = {
  vm_missing: 'VM gone', vm_replaced: 'VM replaced (another instance UUID)', cpu_changed: 'vCPUs changed',
  memory_changed: 'Memory changed', nic_portgroup_changed: 'NIC moved to another port group',
  nic_mac_changed: 'NIC MAC changed', nic_added: 'NIC added', nic_removed: 'NIC removed',
  portgroup_missing: 'Port group gone', portgroup_vlan_changed: 'Port group VLAN changed',
  snapshot_unexpected: 'Snapshot not taken by TrueNorth', snapshot_missing: 'Recorded snapshot gone',
};
/** The kinds TrueNorth deletes after an administrator confirms them (the API's DELETABLE_KINDS). */
const DELETABLE = new Set(['vm', 'portgroup']);

/**
 * Range certification S5 (ADR 0013 §5) on the range page: drift warnings (the live range
 * against what was recorded when it was built) and the last teardown's verification, with
 * the unidentified-items panel. An unidentified item carries the range's mark but was not
 * recorded at clone, so it is never deleted on that mark alone: an administrator types
 * each one's managed-object id back, gives a reason, and the server checks both, audits
 * it and records it as an override. Staff only (`range:certification_read`).
 */
@Component({
  selector: 'tn-range-inventory',
  imports: [DatePipe, MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (shown()) {
      <section class="inv" aria-label="Inventory checks">
        <h3><mat-icon>fact_check</mat-icon> Drift and teardown</h3>
        @if (loading()) {
          <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading inventory checks"></div>
        }
        @if (error()) { <p class="muted" role="status">{{ error() }}</p> }
        @if (data(); as d) {
          @if (!d.drift && !d.teardown) {
            <p class="muted" role="status">No drift check or teardown verification is recorded for this range yet.</p>
          }
          @if (d.drift; as dr) {
            <div class="block" data-check="drift">
              <h4>Drift <span class="state" [attr.data-status]="dr.status">{{ label(dr.status) }}</span></h4>
              <p class="summary" [attr.role]="dr.status === 'pass' ? null : 'alert'">{{ dr.summary }}</p>
              @if (dr.findings?.length) {
                <table class="tn-table">
                  <caption class="sr-only">Changes from the recorded inventory</caption>
                  <thead><tr><th scope="col">Change</th><th scope="col">Where</th><th scope="col">Recorded</th><th scope="col">Now</th></tr></thead>
                  <tbody>
                    @for (f of dr.findings; track $index) {
                      <tr [attr.data-kind]="f.kind">
                        <td>{{ findingLabel(f) }}</td><td>{{ f.object }}</td>
                        <td><code>{{ show(f.expected) }}</code></td><td><code>{{ show(f.actual) }}</code></td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
              <p class="muted small">Checked {{ dr.checked_at | date: 'short' }}; last changed {{ dr.changed_at | date: 'short' }}.</p>
            </div>
          }
          @if (d.teardown; as t) {
            <div class="block" data-check="teardown">
              <h4>Last teardown <span class="state" [attr.data-status]="t.status">{{ label(t.status) }}</span></h4>
              <p class="summary">{{ t.summary }}</p>
              @if (t.remaining?.length) {
                <p class="warn" role="alert">Recorded items still present: {{ itemList(t.remaining ?? []) }}</p>
              }
              @if (t.not_checked?.length) {
                <ul class="muted small">@for (n of t.not_checked; track n) { <li>Not checked: {{ n }}</li> }</ul>
              }
              @if (t.confirmed_deleted?.length) {
                <p class="muted small">Deleted after confirmation: {{ itemList(t.confirmed_deleted ?? []) }}</p>
              }
              @if (t.unidentified?.length) {
                <div class="unidentified" aria-label="Unidentified items">
                  <h4>Unidentified items ({{ t.unidentified!.length }})</h4>
                  <p class="muted small">These carry this range's annotation, tag or owner mark but were not recorded when
                    it was built, so the teardown left them alone. TrueNorth never deletes on a mark alone: to delete one,
                    type its managed-object id exactly. The server checks it against this list and vCenter again, and
                    the confirmation is audited and recorded on the certification record.</p>
                  <table class="tn-table">
                    <caption class="sr-only">Items carrying the range's mark that were not recorded</caption>
                    <thead><tr><th scope="col">Kind</th><th scope="col">Name</th><th scope="col">Managed-object id</th><th scope="col">Marks</th>
                      @if (d.can_confirm) { <th scope="col">Type the id to confirm</th> }</tr></thead>
                    <tbody>
                      @for (u of t.unidentified; track u.moid) {
                        <tr [attr.data-moid]="u.moid">
                          <td>{{ u.kind }}</td><td>{{ u.name }}</td><td><code>{{ u.moid }}</code></td>
                          <td>{{ (u.marks ?? []).join(', ') }}</td>
                          @if (d.can_confirm) {
                            <td>
                              @if (deletable(u)) {
                                <input class="typed" type="text" autocomplete="off" spellcheck="false"
                                       [attr.aria-label]="'Type ' + u.moid + ' to confirm'"
                                       [class.match]="matches(u)" (input)="type(u.moid!, $event)" />
                              } @else { <span class="muted small">Remove by hand</span> }
                            </td>
                          }
                        </tr>
                      }
                    </tbody>
                  </table>
                  @if (d.can_confirm) {
                    <label class="reason">Reason (audited)
                      <textarea rows="2" (input)="setReason($event)" aria-label="Reason"></textarea>
                    </label>
                    <button mat-flat-button color="warn" (click)="confirm()" [disabled]="!canSubmit()">
                      <mat-icon>delete_forever</mat-icon> Delete {{ confirmed().length }} confirmed item(s)
                    </button>
                  } @else if (d.confirm_blocked) {
                    <p class="muted small" role="status">{{ d.confirm_blocked }}</p>
                  }
                </div>
              }
              <p class="muted small">Verified {{ t.checked_at | date: 'short' }}.</p>
            </div>
          }
        }
      </section>
    }
  `,
  styles: [`
    .inv { display: block; }
    h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 8px; font-size: 16px; }
    h4 { margin: 12px 0 4px; font-size: 14px; display: flex; gap: 8px; align-items: center; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .small { font-size: 12px; }
    .summary { font-size: 13px; margin: 0 0 6px; }
    .warn { color: var(--error, #dc2626); font-size: 13px; }
    .state { padding: 1px 8px; border-radius: 10px; font-size: 12px; border: 1px solid; font-weight: 600; }
    .state[data-status='pass'] { color: var(--success, #16a34a); }
    .state[data-status='fail'] { color: var(--error, #dc2626); }
    .state[data-status='incomplete'], .state[data-status='error'] { color: var(--warning, #b45309); }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: 13px; }
    .typed { font-family: var(--font-mono); font-size: 12px; width: 14em; }
    .typed.match { outline: 2px solid var(--error, #dc2626); }
    .reason { display: flex; flex-direction: column; gap: 4px; margin: 8px 0; font-size: 13px; max-width: 520px; }
    code { font-family: var(--font-mono); font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeInventoryComponent {
  private api = inject(RangeInventoryApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);

  readonly rangeId = input.required<string>();
  readonly shown = computed(() => roleCan(this.auth.user()?.role, 'range:certification_read'));

  readonly data = signal<RangeInventoryChecks | null>(null);
  readonly loading = signal(false);
  readonly busy = signal(false);
  readonly error = signal('');
  /** What the administrator typed per listed moid. */
  readonly typed = signal<Record<string, string>>({});
  readonly reason = signal('');
  /** Items whose typed id repeats their moid exactly (no trimming, no case folding: the API's rule). */
  readonly confirmed = computed(() =>
    (this.data()?.teardown?.unidentified ?? []).filter(u => this.deletable(u) && this.typed()[u.moid ?? ''] === u.moid));
  readonly canSubmit = computed(() =>
    !!this.data()?.can_confirm && !this.busy() && this.confirmed().length > 0 && this.reason().trim().length >= 10);

  constructor() {
    effect(() => {
      const id = this.rangeId();
      if (!this.shown()) return;
      untracked(() => this.load(id));
    });
  }

  label(status: string): string { return STATUS_LABEL[status] ?? status; }
  findingLabel(f: DriftFinding): string { return FINDING_LABEL[f.kind] ?? f.kind; }
  show(value: unknown): string { return value === null || value === undefined ? '—' : String(value); }
  deletable(u: InventoryItem): boolean { return DELETABLE.has(u.kind) && !!u.moid; }
  matches(u: InventoryItem): boolean { return this.typed()[u.moid ?? ''] === u.moid; }
  itemList(items: InventoryItem[]): string {
    return items.map(i => `${i.kind} ${i.path ?? i.moid ?? i.id ?? ''}${i.name ? ' (' + i.name + ')' : ''}`).join('; ');
  }

  type(moid: string, event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    this.typed.update(t => ({ ...t, [moid]: value }));
  }

  setReason(event: Event): void { this.reason.set((event.target as HTMLTextAreaElement).value); }

  load(id = this.rangeId()): void {
    this.loading.set(true);
    this.error.set('');
    this.api.get(id).subscribe({
      next: res => {
        if (id !== this.rangeId()) return;
        this.data.set(res);
        this.loading.set(false);
      },
      error: (err: unknown) => {
        if (id !== this.rangeId()) return;
        this.loading.set(false);
        this.data.set(null);
        this.error.set(describe(err));
      },
    });
  }

  confirm(): void {
    if (!this.canSubmit()) return;
    const id = this.rangeId();
    const items = this.confirmed().map(u => ({ moid: u.moid!, confirm_moid: this.typed()[u.moid!] }));
    this.busy.set(true);
    this.api.confirmUnidentified(id, items, this.reason()).subscribe({
      next: res => {
        this.busy.set(false);
        this.typed.set({});
        this.reason.set('');
        if (res.dispatched) this.notify.success(res.message);
        else this.notify.error(res.message);
        this.load(id);
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.notify.error(describe(err));
        this.load(id);
      },
    });
  }
}

function describe(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (err.status === 404) return 'Range not found (or not in your organisation).';
    if (err.status === 403) return 'Your role cannot do this.';
    if (err.status === 0) return 'The API could not be reached.';
    const detail = err.error?.detail;
    if (typeof detail === 'string') return detail;
    if (detail && Array.isArray(detail.problems)) return `${detail.message}: ${detail.problems.join('; ')}`;
    return `The API answered ${err.status}.`;
  }
  return 'The inventory checks could not be loaded.';
}
