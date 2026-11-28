// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
import { Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { RateCard, UsageApiService } from '@core/services/usage-api.service';
import type { Tenant } from '@core/models';
import { apiError } from '../ranges/range-capabilities';
import {
  HOURS_PER_MONTH, Helper, RateForm, inForce, isoToLocal, money, rate, rateCardBody, suggestRates,
} from './usage-cost.util';

interface Scope { id: string | null; name: string; cards: RateCard[]; current: RateCard | null }

/**
 * The rate cards (ADR 0021): the platform default and each organisation's own, newest first.
 * A card is never edited; a new one takes over from its date, so cost already reported keeps
 * the price it was run at. Adding one needs usage:admin; the platform default, the platform
 * administrator.
 */
@Component({
  selector: 'tn-rate-cards',
  imports: [DatePipe, FormsModule],
  template: `
    <div class="split" [class.solo]="!form()">
      <div class="stack">
        <section class="panel hero">
          <span class="eyebrow">Rate cards</span>
          <h2>A card is never edited. A new one takes over from its date.</h2>
          <p>Cost already reported keeps the rates it was priced with: an interval that crosses a card's date is split,
            and each side pays its own card. An organisation's card replaces the platform default for it.</p>
          @if (canAdd()) {
            <button type="button" class="action primary" (click)="open()" [disabled]="!!form()">New rate card</button>
          }
        </section>
        @if (error()) { <p class="error" role="alert">{{ error() }}</p> }
        @for (s of scopes(); track s.id) {
          <section class="panel">
            <h2>{{ s.name }}</h2>
            @for (c of s.cards; track c.id) {
              <div class="row">
                <div>
                  <strong>From {{ c.effective_from | date:'d MMM y, HH:mm' }}</strong>
                  @if (c.id === s.current?.id) { <span class="chip ok">in force</span> }
                  @else if (isFuture(c)) { <span class="chip neutral">scheduled</span> }
                  @if (c.notes) { <p class="small muted">{{ c.notes }}</p> }
                </div>
                <div class="rates small">
                  {{ c.currency }} · vCPU {{ rate(c.per_vcpu_hour) }}/h · RAM {{ rate(c.per_gb_ram_hour) }}/GB-h<br>
                  disk {{ rate(+c.per_gb_disk_hour * perMonth, 3) }}/GB-month · VM {{ rate(c.per_vm_hour) }}/h
                </div>
              </div>
            } @empty {
              <p class="small muted">{{ s.id ? 'No card of its own: it pays the platform default.' : 'No platform default yet: time with no card is counted, not priced.' }}</p>
            }
          </section>
        }
      </div>

      @if (form(); as f) {
        <aside class="panel">
          <div class="head"><h2>New rate card</h2><button type="button" class="link" (click)="form.set(null)">Close</button></div>
          @if (platform()) {
            <label class="field"><span>Applies to</span>
              <select class="input" [(ngModel)]="f.platformWide">
                <option [ngValue]="true">Platform default (every organisation without its own card)</option>
                <option [ngValue]="false">{{ ownName() }}</option>
              </select></label>
          } @else {
            <p class="small muted">Applies to {{ ownName() }}. Only the platform administrator sets the platform default.</p>
          }
          <label class="field"><span>In force from</span>
            <input class="input" type="datetime-local" [(ngModel)]="f.effectiveFrom">
            <small>Your time zone ({{ tz }}). Now or later: cost already reported never changes.</small></label>
          <div class="two">
            <label class="field"><span>per vCPU-hour</span><input class="input" inputmode="decimal" [(ngModel)]="f.vcpu"></label>
            <label class="field"><span>per GB-RAM-hour</span><input class="input" inputmode="decimal" [(ngModel)]="f.ram"></label>
            <label class="field"><span>per GB-disk-month</span><input class="input" inputmode="decimal" [(ngModel)]="f.diskMonth">
              <small>Stored per hour (÷ {{ perMonth }}).</small></label>
            <label class="field"><span>per VM-hour</span><input class="input" inputmode="decimal" [(ngModel)]="f.vm">
              <small>Licences; 0 if none.</small></label>
          </div>
          <label class="field"><span>Currency</span><input class="input" maxlength="3" [(ngModel)]="f.currency"></label>
          <label class="field"><span>Notes</span><input class="input" maxlength="1000" placeholder="Why the rates changed" [(ngModel)]="f.notes"></label>

          <details class="help">
            <summary><strong>Work out rates from the hardware</strong> <span class="small muted">(a helper; it stores nothing)</span></summary>
            <div class="two">
              <label class="field"><span>Hardware cost</span><input class="input" type="number" min="0" [ngModel]="helper().hardware" (ngModelChange)="setHelper('hardware', $event)"></label>
              <label class="field"><span>Paid off over (years)</span><input class="input" type="number" min="1" [ngModel]="helper().years" (ngModelChange)="setHelper('years', $event)"></label>
              <label class="field"><span>Power, cooling, support per month</span><input class="input" type="number" min="0" [ngModel]="helper().monthly" (ngModelChange)="setHelper('monthly', $event)"></label>
              <label class="field"><span>Expected use (%)</span><input class="input" type="number" min="1" max="100" [ngModel]="helper().use" (ngModelChange)="setHelper('use', $event)"></label>
              <label class="field"><span>vCPU capacity</span><input class="input" type="number" min="1" [ngModel]="helper().vcpu" (ngModelChange)="setHelper('vcpu', $event)"></label>
              <label class="field"><span>RAM capacity (GB)</span><input class="input" type="number" min="1" [ngModel]="helper().ramGb" (ngModelChange)="setHelper('ramGb', $event)"></label>
              <label class="field"><span>Disk capacity (TB)</span><input class="input" type="number" min="1" [ngModel]="helper().diskTb" (ngModelChange)="setHelper('diskTb', $event)"></label>
              <label class="field"><span>Share of cost: CPU / RAM (%)</span>
                <span class="pair"><input class="input" type="number" min="0" max="100" aria-label="CPU share" [ngModel]="helper().shareCpu" (ngModelChange)="setHelper('shareCpu', $event)">
                <input class="input" type="number" min="0" max="100" aria-label="RAM share" [ngModel]="helper().shareRam" (ngModelChange)="setHelper('shareRam', $event)"></span>
                <small>Disk carries the rest.</small></label>
            </div>
            @if (suggested(); as s) {
              <p class="small">Recover {{ money(s.perHour) }} an hour → vCPU {{ s.vcpu.toFixed(4) }}/h, RAM {{ s.ram.toFixed(4) }}/GB-h,
                disk {{ s.diskMonth.toFixed(3) }}/GB-month.</p>
              <button type="button" class="link" (click)="useSuggested()">Use these rates</button>
            } @else { <p class="small muted">Fill in every capacity and the years to see rates.</p> }
          </details>

          @if (formError()) { <p class="error" role="alert">{{ formError() }}</p> }
          <div class="actions">
            <button type="button" class="action primary" (click)="save()" [disabled]="busy()">{{ busy() ? 'Saving…' : 'Save rate card' }}</button>
            <span class="small muted">Recorded in the audit log.</span>
          </div>
        </aside>
      }
    </div>
  `,
  styles: [`
    :host { display:block; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:16px; align-items:start; }
    .split.solo { grid-template-columns:minmax(0,1fr); }
    .stack { display:grid; gap:16px; align-content:start; }
    .panel { background:#fff; border:1px solid var(--tn-line); border-radius:10px; padding:18px; min-width:0; }
    .hero { background:var(--tn-soft); border:0; }
    .hero h2 { font-size:20px; letter-spacing:-.3px; margin:7px 0 9px; }
    h2 { font-size:17px; font-weight:600; margin:0 0 12px; }
    .eyebrow { font-size:11px; font-weight:600; letter-spacing:1.2px; text-transform:uppercase; color:var(--tn-accent); }
    .muted { color:var(--tn-muted); } .small { font-size:12px; }
    .row { display:flex; justify-content:space-between; gap:14px; padding:11px 0; border-top:1px solid var(--tn-line); }
    .row:first-of-type { border-top:0; padding-top:0; }
    .row p { margin:2px 0 0; }
    .rates { text-align:right; font-variant-numeric:tabular-nums; }
    .chip { display:inline-block; margin-left:6px; padding:2px 7px; border-radius:5px; font-size:11px; font-weight:500; }
    .chip.ok { background:#e6f3ec; color:#1f6b45; }
    .chip.neutral { background:var(--tn-bg); color:var(--tn-muted); }
    .action { border:1px solid var(--tn-line); border-radius:7px; padding:8px 13px; background:#fff; cursor:pointer; font:inherit; color:inherit; }
    .action.primary { background:#bd1631; color:#fff; border-color:transparent; }
    .action[disabled] { opacity:.5; cursor:not-allowed; }
    .head { display:flex; justify-content:space-between; align-items:flex-start; }
    .link { border:0; background:transparent; color:var(--tn-accent); padding:4px 0; cursor:pointer; font:inherit; }
    .field { display:block; margin-bottom:12px; }
    .field > span { display:block; font-weight:500; margin-bottom:4px; font-size:13px; }
    .field small { display:block; color:var(--tn-muted); margin-top:3px; font-size:12px; }
    .input { width:100%; box-sizing:border-box; background:#fff; border:1px solid var(--tn-line); border-radius:7px; padding:7px 9px; font:inherit; color:inherit; }
    .two { display:grid; grid-template-columns:1fr 1fr; gap:0 10px; }
    .pair { display:flex; gap:6px; }
    .help { background:var(--tn-bg); border:1px solid var(--tn-line); border-radius:8px; padding:14px; margin:4px 0 12px; }
    .help summary { cursor:pointer; margin-bottom:10px; }
    .actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .error { color:var(--tn-accent); }
    @media (max-width:1100px) { .split { grid-template-columns:1fr; } }
    @media (max-width:550px) { .two { grid-template-columns:1fr; } .row { flex-direction:column; } .rates { text-align:left; } }
  `],
})
export class RateCardsComponent implements OnInit {
  private readonly usage = inject(UsageApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);

  /** Organisation names, from the page (GET /tenants). */
  readonly tenants = input<Tenant[]>([]);

  readonly money = money;
  readonly rate = rate;
  readonly perMonth = HOURS_PER_MONTH;
  readonly tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  readonly cards = signal<RateCard[]>([]);
  readonly error = signal<string | null>(null);
  readonly form = signal<RateForm | null>(null);
  readonly formError = signal<string | null>(null);
  readonly busy = signal(false);
  readonly helper = signal<Helper>({
    hardware: 0, years: 5, monthly: 0, use: 60, vcpu: 0, ramGb: 0, diskTb: 0, shareCpu: 50, shareRam: 35,
  });

  readonly platform = computed(() => this.auth.isPlatformAdmin());
  readonly canAdd = computed(() => this.auth.can('usage:admin'));
  readonly suggested = computed(() => suggestRates(this.helper()));
  /** The caller's organisation: /auth/me's user, or the only one GET /tenants returns. */
  readonly ownTenant = computed<string | null>(() => {
    const id = this.auth.me()?.user?.['tenant_id'];
    if (typeof id === 'string') return id;
    const t = this.tenants();
    return t.length === 1 ? t[0].id : null;
  });
  readonly ownName = computed(() => {
    const own = this.ownTenant();
    return this.tenants().find(t => t.id === own)?.name ?? 'Your organisation';
  });

  /** The platform default first, then each organisation that has a card (or is the caller's). */
  readonly scopes = computed<Scope[]>(() => {
    const cards = [...this.cards()].sort((a, b) => b.effective_from.localeCompare(a.effective_from));
    const own = this.ownTenant();
    const ids = [...new Set([...(own ? [own] : []), ...cards.map(c => c.tenant_id).filter((t): t is string => !!t)])];
    const name = (id: string) => this.tenants().find(t => t.id === id)?.name ?? (id === own ? 'Your organisation' : 'Another organisation');
    const scope = (id: string | null): Scope => ({
      id, name: id ? name(id) : 'Platform default', cards: cards.filter(c => c.tenant_id === id), current: inForce(cards, id),
    });
    return [scope(null), ...ids.map(scope)];
  });

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.usage.rateCards().subscribe({
      next: c => { this.cards.set(c); this.error.set(null); },
      error: err => this.error.set(apiError(err)),
    });
  }

  isFuture(c: RateCard): boolean {
    return Date.parse(c.effective_from) > Date.now();
  }

  open(): void {
    const start = new Date();
    start.setDate(start.getDate() + 1);
    start.setHours(0, 0, 0, 0);
    const own = this.ownTenant();
    const base = inForce(this.cards(), own) ?? inForce(this.cards(), null);
    this.formError.set(null);
    this.form.set({
      effectiveFrom: isoToLocal(start.toISOString()),
      currency: base?.currency ?? 'CAD',
      vcpu: base ? String(+base.per_vcpu_hour) : '0',
      ram: base ? String(+base.per_gb_ram_hour) : '0',
      diskMonth: base ? (+base.per_gb_disk_hour * HOURS_PER_MONTH).toFixed(4) : '0',
      vm: base ? String(+base.per_vm_hour) : '0',
      notes: '',
      platformWide: this.platform(),
    });
  }

  setHelper(key: keyof Helper, value: number | string): void {
    this.helper.update(h => ({ ...h, [key]: Number(value) || 0 }));
  }

  useSuggested(): void {
    const s = this.suggested(), f = this.form();
    if (!s || !f) return;
    this.form.set({ ...f, vcpu: s.vcpu.toFixed(6), ram: s.ram.toFixed(6), diskMonth: s.diskMonth.toFixed(4) });
  }

  save(): void {
    const f = this.form();
    if (!f) return;
    const body = rateCardBody(f);
    if (typeof body === 'string') { this.formError.set(body); return; }
    this.busy.set(true);
    this.usage.createRateCard(body).subscribe({
      next: () => {
        this.busy.set(false);
        this.form.set(null);
        this.notify.success('Rate card saved');
        this.load();
      },
      error: err => { this.busy.set(false); this.formError.set(apiError(err)); },
    });
  }
}
