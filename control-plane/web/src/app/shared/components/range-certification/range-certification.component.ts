import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { roleCan } from '@core/auth/permissions';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CertificationRecord, CertificationSection, IsolationZone, RangeCertification, RangeCertificationApiService,
} from '@core/services/range-certification-api.service';

const STATUS_LABEL: Record<CertificationSection['status'], string> = {
  pass: 'Pass', fail: 'Fail', incomplete: 'Incomplete', missing: 'Missing', info: 'Recorded',
};
const STATUS_ICON: Record<CertificationSection['status'], string> = {
  pass: 'check_circle', fail: 'cancel', incomplete: 'pending', missing: 'help_outline', info: 'info',
};
/** ADR 0013 §4 result classes. */
const RESULT_LABEL: Record<string, string> = {
  proven_unreachable: 'proven unreachable', reachable: 'REACHABLE', inconclusive: 'inconclusive',
  connect_failed_only: 'connect failed only',
};
const ZONE_STATUS: Record<string, string> = {
  no_probe: 'No probe VM', guest_op_failed: 'Guest operations failed', budget_exhausted: 'Out of time',
  not_run: 'Not run',
};

/**
 * The Range Certification Record card (ADR 0013 §6) on the range page: the newest
 * build's verdict, why, each section's latest entry, and the signed exports. Superseded
 * entries are behind a collapsed History link; earlier builds' records are under
 * "Archived certifications" (owner decision 2026-10-21). Staff only (`range:certification_read`);
 * nobody else is shown the card. The verdict is the API's, never computed here.
 */
@Component({
  selector: 'tn-range-certification',
  imports: [DatePipe, MatButtonModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (shown()) {
      <section class="cert" aria-label="Certification">
        <header class="head">
          <h3><mat-icon>verified_user</mat-icon> Certification</h3>
          <div class="actions">
            @if (canCapture()) {
              <button mat-stroked-button (click)="capture()" [disabled]="busy()"
                      matTooltip="Append the evidence TrueNorth holds now. Nothing recorded is ever changed.">
                <mat-icon>add_task</mat-icon> Record evidence
              </button>
            }
            @if (record()) {
              <button mat-stroked-button (click)="download('json')" [disabled]="busy()"
                      matTooltip="Signed JSON: the copy that verifies">
                <mat-icon>download</mat-icon> Export JSON
              </button>
              <button mat-stroked-button (click)="download('pdf')" [disabled]="busy()">
                <mat-icon>picture_as_pdf</mat-icon> Export PDF
              </button>
            }
          </div>
        </header>

        @if (loading()) {
          <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading certification"></div>
        }
        @if (error()) {
          <p class="muted" role="status">{{ error() }}</p>
        }
        @if (data(); as d) {
          @if (d.record; as r) {
            <p class="verdict" [attr.data-verdict]="r.verdict" role="status">
              <span class="badge" [attr.data-verdict]="r.verdict">{{ r.verdict_label }}</span>
              <span class="muted">Build {{ r.generation }}, opened {{ r.opened_at | date: 'medium' }} by {{ r.opened_by }}</span>
              @if (buildNote(r.deployment_status); as note) { <span class="build-note">{{ note }}</span> }
            </p>
            @if (r.reasons.length) {
              <ul class="reasons">
                @for (reason of r.reasons; track reason) { <li>{{ reason }}</li> }
              </ul>
            }
            <table class="tn-table">
              <caption class="sr-only">Certification sections</caption>
              <thead>
                <tr><th scope="col">Section</th><th scope="col">State</th><th scope="col">Evidence</th><th scope="col">Updated</th></tr>
              </thead>
              <tbody>
                @for (s of r.sections; track s.name) {
                  <tr [attr.data-section]="s.name">
                    <td>{{ s.title }}@if (!s.required) { <span class="muted"> (optional)</span> }</td>
                    <td>
                      <span class="state" [attr.data-status]="s.status">
                        <mat-icon inline>{{ icon(s) }}</mat-icon> {{ label(s) }}
                      </span>
                    </td>
                    <td class="summary">
                      @if (s.summary) { {{ s.summary }} }
                      @else if (s.status === 'missing') { <span class="muted">Not recorded yet ({{ s.provided_by }})</span> }
                      @else if (s.name === 'overrides') { <span class="muted">None</span> }
                    </td>
                    <td class="muted">{{ s.updated_at ? (s.updated_at | date: 'short') : '—' }}</td>
                  </tr>
                }
              </tbody>
            </table>
            @if (isolation(); as iso) {
              <div class="isolation" aria-label="Isolation evidence per zone">
                <div class="iso-head">
                  <h4>Isolation evidence per zone</h4>
                  @if (canCapture()) {
                    <button mat-stroked-button (click)="reprobe()" [disabled]="busy()"
                            matTooltip="Run the probes again from every zone (a ready range only). The range's state is never changed.">
                      <mat-icon>network_check</mat-icon> Probe again
                    </button>
                  }
                </div>
                <p class="muted small">
                  Mode: <strong>{{ iso.mode }}</strong>{{ iso.mode === 'enforce' ? ' (a range that is not isolated fails its build)' : ' (recorded; does not block a build)' }}.
                  @if (iso.ranAt) { Probed {{ iso.ranAt | date: 'medium' }}. }
                </p>
                <table class="tn-table">
                  <caption class="sr-only">Isolation evidence per zone</caption>
                  <thead>
                    <tr><th scope="col">Zone</th><th scope="col">Probe</th><th scope="col">Self-test</th><th scope="col">Checks</th><th scope="col">Result</th></tr>
                  </thead>
                  <tbody>
                    @for (z of iso.zones; track z.zone) {
                      <tr [attr.data-zone]="z.zone">
                        <td>{{ z.zone }}@if (z.cidr) { <span class="muted"> {{ z.cidr }}</span> }</td>
                        <td>
                          @if (z.probe; as p) {
                            {{ p.vm }} <span class="muted">({{ p.family }}, {{ p.account }}{{ p.admin ? ', admin' : '' }})</span>
                          } @else { <span class="muted">{{ zoneStatus(z.status) }}</span> }
                        </td>
                        <td>{{ z.status === 'probed' ? (z.selftest?.passed ? 'passed' : 'failed') : '—' }}</td>
                        <td class="small">{{ counts(z) }}</td>
                        <td>
                          <span class="state" [attr.data-status]="zoneState(z)">{{ zoneLabel(z) }}</span>
                          @if (z.reasons?.length) { <div class="muted small">{{ z.reasons!.join('; ') }}</div> }
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>
            }
            <p class="muted small">Plan hash ({{ r.plan_hash_basis }}): <code>{{ r.plan_hash.slice(0, 16) }}…</code>.
              Records are never edited; corrections are appended.</p>
            @if (superseded() > 0) {
              <button mat-button class="link" (click)="toggleHistory()" [attr.aria-expanded]="historyOpen()">
                <mat-icon>{{ historyOpen() ? 'expand_less' : 'expand_more' }}</mat-icon>
                History ({{ superseded() }} earlier {{ superseded() === 1 ? 'entry' : 'entries' }})
              </button>
              @if (historyOpen()) {
                @if (history(); as h) {
                  <div class="history" aria-label="Certification history">
                    @for (s of h.sections; track s.name) {
                      @if (s.entries.length > 1) {
                        <h4>{{ s.title }}</h4>
                        <ol>
                          @for (e of s.entries; track e.seq) {
                            <li [attr.data-status]="e.status">
                              <span class="muted">#{{ e.seq }} {{ e.recorded_at | date: 'short' }}</span>
                              {{ e.status }}: {{ e.summary }}
                              <span class="muted">({{ e.actor }}@if (e.reason) {; {{ e.reason }}}@if (e.corrects_seq) {; corrects #{{ e.corrects_seq }}})</span>
                            </li>
                          }
                        </ol>
                      }
                    }
                  </div>
                } @else {
                  <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading history"></div>
                }
              }
            }
          } @else {
            <p class="muted" role="status">No build of this range has a certification record yet. One is opened each time it is provisioned.</p>
          }
          @if (d.archived.length) {
            <details class="archived">
              <summary>Archived certifications ({{ d.archived.length }})</summary>
              <p class="muted small">
                @if (d.retention_months > 0) {
                  Records of earlier builds are deleted {{ d.retention_months }} months after they were opened. Export one to keep it.
                } @else {
                  Records of earlier builds are kept.
                }
              </p>
              <table class="tn-table">
                <caption class="sr-only">Archived certifications</caption>
                <thead>
                  <tr><th scope="col">Build</th><th scope="col">Opened</th><th scope="col">Verdict</th><th scope="col">Deleted after</th><th scope="col">Export</th></tr>
                </thead>
                <tbody>
                  @for (a of d.archived; track a.deployment_id) {
                    <tr [attr.data-deployment]="a.deployment_id">
                      <td>{{ a.generation }}</td>
                      <td>{{ a.opened_at | date: 'medium' }}</td>
                      <td><span class="badge" [attr.data-verdict]="a.verdict">{{ a.verdict_label }}</span></td>
                      <td class="muted">{{ a.purge_after ? (a.purge_after | date: 'mediumDate') : 'kept' }}</td>
                      <td>
                        <button mat-button (click)="download('json', a.deployment_id)" [disabled]="busy()"
                                [attr.aria-label]="'Export build ' + a.generation + ' as signed JSON'">
                          <mat-icon>download</mat-icon> JSON
                        </button>
                      </td>
                    </tr>
                  }
                </tbody>
              </table>
            </details>
          }
        }
      </section>
    }
  `,
  styles: [`
    .cert { display: block; }
    .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    h3 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 16px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .small { font-size: 12px; }
    .verdict { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 12px 0 4px; }
    .badge { padding: 2px 10px; border-radius: 12px; font-weight: 600; font-size: 13px; border: 1px solid; }
    .badge[data-verdict='certified'] { color: var(--success, #16a34a); border-color: var(--success, #16a34a); }
    .badge[data-verdict='ready_not_certified'] { color: var(--warning, #b45309); border-color: var(--warning, #f59e0b); }
    .badge[data-verdict='not_safe'] { color: var(--error, #dc2626); border-color: var(--error, #dc2626); }
    .build-note { font-size: 13px; font-weight: 600; }
    .reasons { margin: 4px 0 8px; padding-left: 20px; font-size: 13px; }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .state { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; font-size: 13px; }
    .state[data-status='pass'] { color: var(--success, #16a34a); }
    .state[data-status='fail'] { color: var(--error, #dc2626); }
    .state[data-status='incomplete'], .state[data-status='missing'] { color: var(--warning, #b45309); }
    .summary { font-size: 13px; }
    .link { margin-top: 4px; }
    .history h4 { margin: 8px 0 2px; font-size: 13px; }
    .history ol { margin: 0; padding-left: 20px; font-size: 13px; }
    .isolation { margin: 12px 0 4px; }
    .iso-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .iso-head h4 { margin: 0; font-size: 14px; }
    .archived { margin-top: 12px; }
    .archived summary { cursor: pointer; font-size: 14px; }
    code { font-family: var(--font-mono); font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeCertificationComponent {
  private api = inject(RangeCertificationApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);

  readonly rangeId = input.required<string>();

  readonly shown = computed(() => roleCan(this.auth.user()?.role, 'range:certification_read'));
  readonly canCapture = computed(() => this.shown() && roleCan(this.auth.user()?.role, 'range:provision'));

  readonly data = signal<RangeCertification | null>(null);
  readonly record = computed(() => this.data()?.record ?? null);
  readonly loading = signal(false);
  readonly busy = signal(false);
  readonly error = signal('');
  readonly historyOpen = signal(false);
  readonly history = signal<CertificationRecord | null>(null);
  /** The isolation section's per-zone evidence (S4), from its latest entry's detail. */
  readonly isolation = computed(() => {
    const s = this.record()?.sections.find(x => x.name === 'isolation');
    const detail = s?.entries?.[0]?.detail as Record<string, unknown> | undefined;
    const zones = detail?.['zones'];
    if (!Array.isArray(zones)) return null;
    return {
      mode: typeof detail?.['mode'] === 'string' ? detail['mode'] as string : 'warn',
      ranAt: typeof detail?.['ran_at'] === 'string' ? detail['ran_at'] as string : null,
      zones: zones as IsolationZone[],
    };
  });
  /** Entries hidden from the card: every section's entries but its latest. */
  readonly superseded = computed(() =>
    (this.record()?.sections ?? []).reduce((n, s) => n + Math.max(0, s.history_count - 1), 0));

  constructor() {
    effect(() => {
      const id = this.rangeId();
      if (!this.shown()) return;
      untracked(() => this.load(id));
    });
  }

  label(s: CertificationSection): string { return STATUS_LABEL[s.status]; }

  /** The build's own state, when the verdict alone would mislead (still building, failed). */
  buildNote(status: string): string {
    if (status === 'pending' || status === 'dispatched') return 'Build in progress';
    if (status === 'held') return 'Build held by a maintenance window';
    if (status === 'failed') return 'Build failed';
    return '';
  }
  icon(s: CertificationSection): string { return STATUS_ICON[s.status]; }

  zoneStatus(status: string): string { return ZONE_STATUS[status] ?? status; }

  /** The zone's grade as a section status, for the colour. */
  zoneState(z: IsolationZone): CertificationSection['status'] {
    return z.verdict === 'isolated' ? 'pass' : z.verdict === 'not_isolated' ? 'fail' : 'incomplete';
  }

  zoneLabel(z: IsolationZone): string {
    return z.verdict === 'isolated' ? 'Isolated' : z.verdict === 'not_isolated' ? 'Not isolated' : 'Not proven';
  }

  /** "12 proven unreachable, 1 connect failed only" — how each graded check came out. */
  counts(z: IsolationZone): string {
    const n = new Map<string, number>();
    for (const c of z.checks ?? []) {
      if (c.expect === 'precondition') continue;
      const key = c.expect === 'reachable' ? (c.result === 'reachable' ? 'exceptions connected' : 'exceptions failed')
        : RESULT_LABEL[c.result] ?? c.result;
      n.set(key, (n.get(key) ?? 0) + 1);
    }
    return [...n.entries()].map(([k, v]) => `${v} ${k}`).join(', ') || '—';
  }

  reprobe(): void {
    const id = this.rangeId();
    this.busy.set(true);
    this.api.checkIsolation(id).subscribe({
      next: res => {
        this.busy.set(false);
        if (res.dispatched) this.notify.success('Isolation probes started; the record shows the result when they finish');
        else this.notify.error('The task queue could not be reached; try again later');
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.notify.error(describe(err));
      },
    });
  }

  toggleHistory(): void {
    const open = !this.historyOpen();
    this.historyOpen.set(open);
    const r = this.record();
    if (!open || !r || this.history()) return;
    const id = this.rangeId();
    this.api.get(id, { deploymentId: r.deployment_id, history: true }).subscribe({
      next: res => { if (id === this.rangeId()) this.history.set(res.record ?? null); },
      error: (err: unknown) => this.notify.error(describe(err)),
    });
  }

  load(id = this.rangeId()): void {
    this.history.set(null);
    this.historyOpen.set(false);
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

  capture(): void {
    const id = this.rangeId();
    this.busy.set(true);
    this.api.capture(id).subscribe({
      next: res => {
        this.busy.set(false);
        this.notify.success(res.written ? `${res.written} evidence entr${res.written === 1 ? 'y' : 'ies'} recorded`
          : 'Nothing new to record');
        this.load(id);
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.notify.error(describe(err));
      },
    });
  }

  download(format: 'json' | 'pdf', deploymentId = this.record()?.deployment_id): void {
    if (!deploymentId) return;
    this.busy.set(true);
    this.api.export(this.rangeId(), format, deploymentId).subscribe({
      next: blob => {
        this.busy.set(false);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `range-certification-${deploymentId}.${format}`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.notify.error(describe(err));
      },
    });
  }
}

function describe(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (err.status === 404) return 'Range not found (or not in your organisation).';
    if (err.status === 403) return 'Your role does not see certification records.';
    if (err.status === 0) return 'The API could not be reached.';
    const detail = err.error?.detail;
    if (typeof detail === 'string') return detail;
    return `The API answered ${err.status}.`;
  }
  return 'The certification record could not be loaded.';
}
