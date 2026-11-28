// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CostQuery, CostReport, CostRow, GroupBy, StudentCost, UsageApiService,
} from '@core/services/usage-api.service';
import type { Tenant } from '@core/models';
import { apiError } from '../ranges/range-capabilities';
import { RateCardsComponent } from './rate-cards.component';
import { PERIOD_LABELS, PeriodKey, RangeGroup, hours, isBucket, money, period, rangeGroups } from './usage-cost.util';

type SortCol = 'label' | 'vm' | 'compute' | 'disk' | 'total';

/**
 * Usage and cost (ADR 0021, usage S3): what each Student costs to run on the range,
 * grouped by Student, course, class or tenant, with a Student's drill-down and the rate
 * cards. Admins only (usage:read). Mockup: docs/mockups/usage-cost.html.
 */
@Component({
  selector: 'tn-usage-cost',
  imports: [DatePipe, FormsModule, RateCardsComponent],
  template: `
    <div class="page">
      <span class="eyebrow">Administration</span>
      <h1>Usage and cost</h1>
      <p class="muted lead">What each Student costs to run on the range, at the size each range was built and the rate
        card in force.@if (!platform()) { Your organisation only.}</p>

      <nav class="tabs" aria-label="Usage and cost">
        <button type="button" [attr.aria-current]="tab() === 'cost' ? 'page' : null" (click)="tab.set('cost')">Cost</button>
        <button type="button" [attr.aria-current]="tab() === 'rates' ? 'page' : null" (click)="tab.set('rates')">Rate cards</button>
      </nav>

      @if (tab() === 'rates') {
        <tn-rate-cards [tenants]="tenants()" />
      } @else {
        @if (report(); as r) {
          <section class="panel hero">
            <span class="eyebrow">{{ periodLabel() }} · to {{ r.period_to | date:'d MMM HH:mm' }}</span>
            <div class="big">{{ money(r.totals.cost_total, r.totals.currency) }}
              @if (studentCount() !== null) { <span class="muted unit">{{ studentCount() }} Students</span> }</div>
            <div class="parts">
              <div><span>Compute</span><strong>{{ money(compute(r.totals), r.totals.currency) }}</strong></div>
              <div><span>Disk</span><strong>{{ money(r.totals.cost_disk, r.totals.currency) }}</strong></div>
              @if (unattributed(); as u) { <div><span>Unattributed</span><strong>{{ money(u.cost_total, u.currency) }}</strong></div> }
              @if (highest(); as h) { <div><span>Highest Student</span><strong>{{ h.label }} · {{ money(h.cost_total, h.currency) }}</strong></div> }
              @if (r.totals.unpriced_hours > 0) { <div><span>No rate card</span><strong>{{ hours(r.totals.unpriced_hours) }} unpriced</strong></div> }
            </div>
          </section>
        }

        <div class="bar">
          <div class="grp"><span class="small muted">Group by</span>
            <div class="seg" role="group" aria-label="Group by">
              @for (g of groupings(); track g) {
                <button type="button" [attr.aria-pressed]="groupBy() === g" (click)="setGroup(g)">{{ groupLabel(g) }}</button>
              }
            </div>
          </div>
          <div class="grp">
            <select class="select" aria-label="Period" [ngModel]="periodKey()" (ngModelChange)="periodKey.set($event); load()">
              @for (p of periodKeys; track p) { <option [value]="p">{{ periodLabels[p] }}</option> }
            </select>
            @if (platform()) {
              <select class="select" aria-label="Organisation" [ngModel]="tenantId()" (ngModelChange)="tenantId.set($event); load()">
                <option value="">All organisations</option>
                @for (t of tenants(); track t.id) { <option [value]="t.id">{{ t.name }}</option> }
              </select>
            }
            <button type="button" class="action" (click)="downloadCsv()" [disabled]="busy()">Download CSV</button>
          </div>
        </div>

        <div class="split" [class.solo]="!selected()">
          <section class="panel table-wrap">
            @if (error()) {
              <p class="error" role="alert">{{ error() }}</p>
            } @else if (!report()) {
              <p class="muted">Loading…</p>
            } @else if (rows().length === 0) {
              <p class="muted">No range was built in this period.</p>
            } @else {
              <table class="uc">
                <caption class="sr-only">Cost per {{ groupLabel(groupBy()).toLowerCase() }}</caption>
                <thead><tr>
                  <th scope="col" [attr.aria-sort]="ariaSort('label')"><button type="button" (click)="sortBy('label')">{{ groupLabel(groupBy()) }}{{ arrow('label') }}</button></th>
                  <th scope="col" class="num hide-sm" [attr.aria-sort]="ariaSort('vm')"><button type="button" (click)="sortBy('vm')">VM-hours{{ arrow('vm') }}</button></th>
                  <th scope="col" class="num" [attr.aria-sort]="ariaSort('compute')"><button type="button" (click)="sortBy('compute')">Compute{{ arrow('compute') }}</button></th>
                  <th scope="col" class="num" [attr.aria-sort]="ariaSort('disk')"><button type="button" (click)="sortBy('disk')">Disk{{ arrow('disk') }}</button></th>
                  <th scope="col" class="num" [attr.aria-sort]="ariaSort('total')"><button type="button" (click)="sortBy('total')">Total{{ arrow('total') }}</button></th>
                </tr></thead>
                <tbody>
                  @for (row of rows(); track row.key) {
                    <tr [class.sel]="selected()?.key === row.key" [class.bucket]="bucket(row)">
                      <td>
                        @if (drillable(row)) {
                          <button type="button" class="rowlink" (click)="select(row)" [attr.aria-expanded]="selected()?.key === row.key">{{ row.label }}</button>
                        } @else { {{ row.label }} }
                        @if (platform() && !tenantId() && groupBy() !== 'tenant') { <span class="sub">{{ tenantName(row.tenant_id) }}</span> }
                        @if (row.approximate) { <span class="chip warm" title="Part of this is approximate">≈ approx.</span> }
                        @if (row.open) { <span class="chip neutral" title="A range is still built: its cost runs to now">open</span> }
                        @if (row.unpriced_hours > 0) { <span class="chip">{{ hours(row.unpriced_hours) }} unpriced</span> }
                      </td>
                      <td class="num hide-sm">{{ hours(row.vm_hours) }}</td>
                      <td class="num">{{ money(compute(row), row.currency) }}</td>
                      <td class="num">{{ money(row.cost_disk, row.currency) }}</td>
                      <td class="num"><strong>{{ money(row.cost_total, row.currency) }}</strong></td>
                    </tr>
                  }
                </tbody>
                @if (report(); as r) {
                  <tfoot><tr>
                    <td>Total</td><td class="num hide-sm">{{ hours(r.totals.vm_hours) }}</td>
                    <td class="num">{{ money(compute(r.totals), r.totals.currency) }}</td>
                    <td class="num">{{ money(r.totals.cost_disk, r.totals.currency) }}</td>
                    <td class="num">{{ money(r.totals.cost_total, r.totals.currency) }}</td>
                  </tr></tfoot>
                }
              </table>
              @if (anyApprox()) {
                <p class="note"><strong>≈ Approximate.</strong> Part of this is from before the usage ledger started, when a range
                  is counted as running the whole time, or from a change the hourly check caught late. The rest is exact.</p>
              }
              <p class="small muted foot">Cost is the allocated size, not measured use: a running range pays vCPU, RAM, VMs and
                disk; a stopped one pays disk only; a destroyed one nothing. A shared class range is split evenly across its
                roll call. Unattributed: ranges with no Student, such as bookings, exercises and ranges built by hand.</p>
            }
          </section>

          @if (selected(); as row) {
            <aside class="panel drill" aria-label="Drill-down">
              <div class="drill-head">
                <div><span class="eyebrow">Student</span><h2>{{ row.label }}</h2></div>
                <button type="button" class="link" (click)="selected.set(null)">Close</button>
              </div>
              <p class="drill-total"><span class="big small-big">{{ money(row.cost_total, row.currency) }}</span>
                <span class="muted"> in {{ periodLabel().toLowerCase() }}</span></p>
              @if (drillError()) {
                <p class="error" role="alert">{{ drillError() }}</p>
              } @else if (!groups()) {
                <p class="muted">Loading…</p>
              } @else {
                @for (g of groups(); track g.key) {
                  <div class="group">
                    <h3>{{ g.title }}</h3>
                    <p class="small muted">@if (g.context) { {{ g.context }} · }{{ g.size }} ·
                      {{ g.start | date:'d MMM' }} – {{ g.open ? 'now' : (g.end | date:'d MMM') }}</p>
                    @if (g.running.hours > 0) { <div class="line"><span>Running {{ hours(g.running.hours) }}</span><span>{{ money(g.running.cost) }}</span></div> }
                    @if (g.stopped.hours > 0) { <div class="line"><span>Stopped, disk only {{ hours(g.stopped.hours) }}</span><span>{{ money(g.stopped.cost) }}</span></div> }
                    @if (g.approx.hours > 0) { <div class="line"><span>Counted as running {{ hours(g.approx.hours) }} <span class="chip warm">≈</span></span><span>{{ money(g.approx.cost) }}</span></div> }
                    <div class="line"><strong>Range total</strong><strong>{{ money(g.total) }}</strong></div>
                    @if (stretches()) {
                      <ul class="stretch">
                        @for (iv of g.intervals; track iv.start) {
                          <li><span>{{ iv.start | date:'d MMM HH:mm' }} – {{ iv.open ? 'now' : (iv.end | date:'d MMM HH:mm') }} ·
                            {{ iv.approximate ? 'counted as running ≈' : iv.compute ? 'running' : 'stopped' }}</span>
                            <span>{{ money(iv.cost_total, iv.currency) }}</span></li>
                        }
                      </ul>
                    }
                  </div>
                } @empty { <p class="muted">Nothing in this period.</p> }
                <button type="button" class="link arrow" (click)="stretches.set(!stretches())">{{ stretches() ? 'Hide' : 'Show' }} every stretch</button>
              }
            </aside>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    :host { --tn-bg:#fff7f7; --tn-ink:#292326; --tn-muted:#655b60; --tn-line:#eadbdd; --tn-accent:#b5122b; --tn-soft:#fcecef;
      --tn-warm:#fff0f2; --tn-warm-ink:#961b30; display:block; color:var(--tn-ink); }
    .page { padding:22px; max-width:1320px; }
    h1 { font-size:27px; line-height:1.18; font-weight:600; letter-spacing:-.8px; margin:5px 0 9px; }
    h2 { font-size:17px; font-weight:600; margin:4px 0 0; }
    h3 { font-size:14px; font-weight:600; margin:0 0 4px; }
    .eyebrow { font-size:11px; font-weight:600; letter-spacing:1.2px; text-transform:uppercase; color:var(--tn-accent); }
    .muted { color:var(--tn-muted); } .small { font-size:12px; } .lead { max-width:66ch; margin:0 0 18px; }
    .tabs { display:flex; gap:18px; border-bottom:1px solid var(--tn-line); margin-bottom:18px; }
    .tabs button { border:0; background:transparent; padding:8px 0; cursor:pointer; color:var(--tn-muted); border-bottom:2px solid transparent; margin-bottom:-1px; font:inherit; }
    .tabs button[aria-current="page"] { color:var(--tn-ink); border-color:var(--tn-accent); font-weight:600; }
    .panel { background:#fff; border:1px solid var(--tn-line); border-radius:10px; padding:18px; min-width:0; }
    .hero { background:var(--tn-soft); border:0; margin-bottom:18px; }
    .big { font-size:30px; font-weight:600; letter-spacing:-.8px; line-height:1.1; margin-top:6px; }
    .big .unit { font-size:15px; font-weight:400; letter-spacing:0; }
    .small-big { font-size:24px; }
    .parts { display:flex; gap:22px; flex-wrap:wrap; margin-top:10px; font-size:13px; }
    .parts span { color:var(--tn-muted); } .parts strong { display:block; font-weight:600; font-size:15px; }
    .bar { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
    .grp { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .seg { display:inline-flex; border:1px solid var(--tn-line); border-radius:7px; overflow:hidden; background:#fff; }
    .seg button { border:0; border-left:1px solid var(--tn-line); background:transparent; padding:7px 12px; cursor:pointer; font:inherit; color:inherit; }
    .seg button:first-child { border-left:0; }
    .seg button[aria-pressed="true"] { background:var(--tn-soft); color:var(--tn-accent); font-weight:600; }
    .select, .action { border:1px solid var(--tn-line); border-radius:7px; padding:7px 10px; background:#fff; font:inherit; color:inherit; }
    .action { cursor:pointer; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:16px; align-items:start; }
    .split.solo { grid-template-columns:minmax(0,1fr); }
    .table-wrap { overflow-x:auto; }
    table.uc { width:100%; border-collapse:collapse; font-size:13px; }
    .uc th { text-align:left; font-weight:500; color:var(--tn-muted); padding:0 8px 8px; border-bottom:1px solid var(--tn-line); white-space:nowrap; }
    .uc th button { border:0; background:transparent; padding:0; cursor:pointer; color:inherit; font:inherit; }
    .uc th[aria-sort] button { color:var(--tn-ink); font-weight:600; }
    .uc td { padding:9px 8px; border-bottom:1px solid var(--tn-line); vertical-align:top; }
    .uc .num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .uc tr.sel td { background:var(--tn-soft); }
    .uc tr.bucket td:first-child { color:var(--tn-muted); font-style:italic; }
    .uc tfoot td { font-weight:600; border-top:2px solid var(--tn-line); border-bottom:0; padding-top:10px; }
    .rowlink { border:0; background:transparent; padding:0; font:inherit; color:inherit; cursor:pointer; text-align:left; }
    .rowlink:hover { color:var(--tn-accent); text-decoration:underline; }
    .sub { display:block; color:var(--tn-muted); font-size:12px; font-style:normal; }
    .chip { display:inline-block; padding:2px 7px; border-radius:5px; background:var(--tn-soft); color:var(--tn-accent); font-size:11px; font-weight:500; white-space:nowrap; font-style:normal; }
    .chip.warm { color:var(--tn-warm-ink); background:var(--tn-warm); border:1px solid var(--tn-line); }
    .chip.neutral { background:var(--tn-bg); color:var(--tn-muted); }
    .note { padding:12px 14px; border-left:3px solid var(--tn-accent); background:var(--tn-soft); margin:12px 0 0; font-size:13px; }
    .foot { margin:10px 0 0; }
    .error { color:var(--tn-accent); }
    .drill-head { display:flex; justify-content:space-between; gap:12px; }
    .drill-total { margin:8px 0 14px; }
    .group { border-top:1px solid var(--tn-line); padding-top:12px; margin-top:12px; }
    .group p { margin:0 0 6px; }
    .line { display:flex; justify-content:space-between; gap:12px; font-size:13px; padding:3px 0; }
    .line span:last-child, .line strong:last-child { font-variant-numeric:tabular-nums; white-space:nowrap; }
    .stretch { font-size:12px; color:var(--tn-muted); margin:6px 0 0; padding:0; list-style:none; max-height:180px; overflow:auto; }
    .stretch li { display:flex; justify-content:space-between; gap:10px; padding:2px 0; }
    .link { border:0; background:transparent; color:var(--tn-accent); padding:4px 0; cursor:pointer; font:inherit; align-self:flex-start; }
    .link.arrow::after { content:" →"; }
    .link.arrow { margin-top:12px; }
    @media (max-width:1100px) { .split { grid-template-columns:1fr; } }
    @media (max-width:800px) { .hide-sm { display:none; } .page { padding:14px; } }
  `],
})
export class UsageCostComponent implements OnInit {
  private readonly usage = inject(UsageApiService);
  private readonly core = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);

  readonly money = money;
  readonly hours = hours;
  readonly periodKeys = Object.keys(PERIOD_LABELS) as PeriodKey[];
  readonly periodLabels = PERIOD_LABELS;

  readonly tab = signal<'cost' | 'rates'>('cost');
  readonly groupBy = signal<GroupBy>('student');
  readonly periodKey = signal<PeriodKey>('month');
  readonly tenantId = signal('');
  readonly report = signal<CostReport | null>(null);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly tenants = signal<Tenant[]>([]);
  readonly sort = signal<{ col: SortCol; dir: 1 | -1 }>({ col: 'total', dir: -1 });
  readonly selected = signal<CostRow | null>(null);
  readonly student = signal<StudentCost | null>(null);
  readonly drillError = signal<string | null>(null);
  readonly stretches = signal(false);

  readonly platform = computed(() => this.auth.isPlatformAdmin());
  readonly groupings = computed<GroupBy[]>(() => this.platform() ? ['student', 'course', 'class', 'tenant'] : ['student', 'course', 'class']);
  readonly periodLabel = computed(() => PERIOD_LABELS[this.periodKey()]);

  /** Named rows first, in the chosen order; unattributed, self-serve and the like last. */
  readonly rows = computed(() => {
    const r = this.report();
    if (!r) return [];
    const { col, dir } = this.sort();
    const val = (x: CostRow): number | string => col === 'label' ? x.label
      : col === 'vm' ? x.vm_hours : col === 'compute' ? this.compute(x) : col === 'disk' ? x.cost_disk : x.cost_total;
    return [...r.rows].sort((a, b) => {
      const bucket = Number(isBucket(a)) - Number(isBucket(b));
      if (bucket) return bucket;
      const va = val(a), vb = val(b);
      return typeof va === 'string' ? va.localeCompare(vb as string) * dir : (va - (vb as number)) * dir;
    });
  });
  readonly anyApprox = computed(() => this.rows().some(r => r.approximate));
  private readonly byStudent = computed(() => this.report()?.group_by === 'student');
  readonly studentCount = computed(() => this.byStudent() ? this.rows().filter(r => !isBucket(r)).length : null);
  readonly unattributed = computed(() => {
    if (!this.byStudent()) return null;
    const un = this.rows().filter(r => r.key.startsWith('unattributed:'));
    return un.length ? { cost_total: un.reduce((s, r) => s + r.cost_total, 0), currency: un[0].currency } : null;
  });
  readonly highest = computed(() => this.byStudent()
    ? [...this.rows()].filter(r => !isBucket(r)).sort((a, b) => b.cost_total - a.cost_total)[0] ?? null : null);
  readonly groups = computed<RangeGroup[] | null>(() => {
    const s = this.student();
    return s ? rangeGroups(s.intervals) : null;
  });

  ngOnInit(): void {
    this.core.listTenants().subscribe({ next: t => this.tenants.set(t), error: () => this.tenants.set([]) });
    this.load();
  }

  /** Fixed at each load, so the table, a drill-down and the CSV all cover the same instants
   *  (an open range's cost runs to "now", which would otherwise move between calls). */
  private span = period('month');

  private query(): CostQuery {
    return { ...this.span, groupBy: this.groupBy(), tenantId: this.tenantId() || null };
  }

  load(): void {
    this.span = period(this.periodKey());
    this.selected.set(null);
    this.error.set(null);
    this.report.set(null);
    this.usage.cost(this.query()).subscribe({
      next: r => this.report.set(r),
      error: err => this.error.set(apiError(err)),
    });
  }

  setGroup(g: GroupBy): void {
    if (g === this.groupBy()) return;
    this.groupBy.set(g);
    this.load();
  }

  sortBy(col: SortCol): void {
    const s = this.sort();
    this.sort.set({ col, dir: s.col === col ? (s.dir === 1 ? -1 : 1) : col === 'label' ? 1 : -1 });
  }

  arrow(col: SortCol): string {
    const s = this.sort();
    return s.col === col ? (s.dir < 0 ? ' ↓' : ' ↑') : '';
  }

  ariaSort(col: SortCol): string | null {
    const s = this.sort();
    return s.col === col ? (s.dir < 0 ? 'descending' : 'ascending') : null;
  }

  groupLabel(g: GroupBy): string {
    return { student: 'Student', course: 'Course', class: 'Class', tenant: 'Organisation' }[g];
  }

  bucket(row: CostRow): boolean {
    return isBucket(row);
  }

  /** A named Student's row opens their drill-down. */
  drillable(row: CostRow): boolean {
    return this.report()?.group_by === 'student' && !isBucket(row);
  }

  compute(f: { cost_vcpu: number; cost_ram: number; cost_vm: number }): number {
    return f.cost_vcpu + f.cost_ram + f.cost_vm;
  }

  tenantName(id: string | null): string {
    return this.tenants().find(t => t.id === id)?.name ?? '';
  }

  select(row: CostRow): void {
    if (this.selected()?.key === row.key) { this.selected.set(null); return; }
    this.selected.set(row);
    this.student.set(null);
    this.drillError.set(null);
    const { from, to } = this.span;
    this.usage.student(row.key, from, to).subscribe({
      next: s => { if (this.selected()?.key === row.key) this.student.set(s); },
      error: err => this.drillError.set(apiError(err)),
    });
  }

  downloadCsv(): void {
    this.busy.set(true);
    this.usage.costCsv(this.query()).subscribe({
      next: blob => {
        this.busy.set(false);
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `usage-cost-${this.groupBy()}-${this.periodKey()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: err => { this.busy.set(false); this.notify.error(apiError(err)); },
    });
  }
}
