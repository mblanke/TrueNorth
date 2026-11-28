// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
/**
 * The usage and cost page's arithmetic, kept out of the components so it is tested on its
 * own: periods, the drill-down's per-range summary, the rate-card form and its helper.
 * The API prices everything (ADR 0021); nothing here computes a cost.
 */
import type { CostRow, RateCard, RateCardIn, UsageInterval } from '@core/services/usage-api.service';

export const HOURS_PER_MONTH = 730;
const HOUR = 3_600_000;

export type PeriodKey = 'month' | 'previous' | 'last7';
export const PERIOD_LABELS: Record<PeriodKey, string> = {
  month: 'This month', previous: 'Last month', last7: 'Last 7 days',
};

/** A period as ISO instants: calendar months in the browser's zone, ending now at the latest. */
export function period(key: PeriodKey, now = new Date()): { from: string; to: string } {
  const startOf = (y: number, m: number) => new Date(y, m, 1, 0, 0, 0, 0);
  if (key === 'previous') {
    return { from: startOf(now.getFullYear(), now.getMonth() - 1).toISOString(), to: startOf(now.getFullYear(), now.getMonth()).toISOString() };
  }
  if (key === 'last7') return { from: new Date(now.getTime() - 7 * 24 * HOUR).toISOString(), to: now.toISOString() };
  return { from: startOf(now.getFullYear(), now.getMonth()).toISOString(), to: now.toISOString() };
}

/** A row with no single Student, course or class behind it (unattributed, self-serve, ...). */
export function isBucket(row: Pick<CostRow, 'key'>): boolean {
  return row.key.includes(':');
}

export function money(n: number, currency = 'CAD'): string {
  const code = /^[A-Z]{3}$/.test(currency) ? currency : 'CAD';
  const text = new Intl.NumberFormat('en-CA', { style: 'currency', currency: code, currencyDisplay: 'narrowSymbol' }).format(n);
  return currency === 'mixed' ? `${text} (mixed currencies)` : text;
}

/** A rate as stored (up to 6 decimal places), trimmed to `digits`. */
export function rate(v: string | number, digits = 4): string {
  return Number(v).toFixed(digits);
}

/** An instant as a datetime-local value in the browser's zone. */
export function isoToLocal(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function hours(n: number): string {
  return `${n.toLocaleString('en-CA', { maximumFractionDigits: n < 10 ? 1 : 0 })} h`;
}

export const SOURCE_LABELS: Record<string, string> = {
  lab: 'Self-serve lab', class_seat: 'Seat range', class_shared: 'Shared class range', unattributed: 'Range',
};

export interface Part { hours: number; cost: number }
export interface RangeGroup {
  key: string;
  title: string;
  context: string;
  size: string;
  start: string;
  end: string;
  open: boolean;
  running: Part;
  stopped: Part;
  approx: Part;
  total: number;
  intervals: UsageInterval[];
}

/** One Student's intervals, one group per range: running, stopped (disk only) and approximate
 *  time (charged as running: before the ledger, or a change caught late). Wall-clock hours;
 *  the cost is the Student's share. */
export function rangeGroups(intervals: UsageInterval[]): RangeGroup[] {
  const by = new Map<string, UsageInterval[]>();
  for (const iv of intervals) {
    const key = iv.range_id ?? iv.source_id;
    by.set(key, [...(by.get(key) ?? []), iv]);
  }
  const groups = [...by.entries()].map(([key, ivs]) => {
    ivs.sort((a, b) => a.start.localeCompare(b.start));
    const first = ivs[0];
    const part = (pick: (iv: UsageInterval) => boolean): Part => ivs.filter(pick).reduce(
      (p, iv) => ({ hours: p.hours + (Date.parse(iv.end) - Date.parse(iv.start)) / HOUR, cost: p.cost + iv.cost_total }),
      { hours: 0, cost: 0 });
    const g: RangeGroup = {
      key,
      title: `${SOURCE_LABELS[first.source] ?? 'Range'}${first.range_name ? ` · ${first.range_name}` : ''}`,
      context: [first.course_name, first.class_name].filter(Boolean).join(' · '),
      size: `${plural(first.vms, 'VM')}, ${first.vcpu} vCPU, ${round(first.ram_gb)} GB RAM, ${round(first.disk_gb).toLocaleString('en-CA')} GB disk`,
      start: first.start,
      end: ivs[ivs.length - 1].end,
      open: ivs.some(iv => iv.open),
      running: part(iv => iv.compute && !iv.approximate),
      stopped: part(iv => !iv.compute),
      approx: part(iv => iv.compute && iv.approximate),
      total: ivs.reduce((s, iv) => s + iv.cost_total, 0),
      intervals: ivs,
    };
    return g;
  });
  return groups.sort((a, b) => a.start.localeCompare(b.start));
}

function round(n: number): number {
  return Math.round(n * 10) / 10;
}

function plural(n: number, word: string): string {
  return `${round(n)} ${word}${n === 1 ? '' : 's'}`;
}

// ── Rate cards ───────────────────────────────────────────────────────────────

export interface RateForm {
  effectiveFrom: string; // datetime-local, the browser's zone
  currency: string;
  vcpu: string;          // per vCPU-hour
  ram: string;           // per GB-RAM-hour
  diskMonth: string;     // per GB-disk-month (stored per hour: / 730)
  vm: string;            // per running VM-hour
  notes: string;
  platformWide: boolean;
}

/** The API keeps 6 decimal places. */
export function toHourly(perMonth: number): string {
  return (perMonth / HOURS_PER_MONTH).toFixed(6);
}

/** The request body for the form, or why it cannot be sent yet. */
export function rateCardBody(f: RateForm, now = new Date()): RateCardIn | string {
  if (!f.effectiveFrom) return 'Set when the card takes over.';
  const from = new Date(f.effectiveFrom);
  if (Number.isNaN(from.getTime())) return 'Set when the card takes over.';
  if (from.getTime() < now.getTime() - 60_000) return 'A card starts now or later: cost already reported never changes.';
  const nums = { vcpu: f.vcpu, ram: f.ram, diskMonth: f.diskMonth, vm: f.vm };
  for (const [k, v] of Object.entries(nums)) {
    const n = Number(v);
    if (v.trim() === '' || !Number.isFinite(n) || n < 0) return `Give a rate of 0 or more (${k}).`;
  }
  if (!/^[A-Z]{3}$/.test(f.currency)) return 'The currency is a 3-letter code, e.g. CAD.';
  return {
    effective_from: from.toISOString(),
    currency: f.currency,
    per_vcpu_hour: Number(f.vcpu).toFixed(6),
    per_gb_ram_hour: Number(f.ram).toFixed(6),
    per_gb_disk_hour: toHourly(Number(f.diskMonth)),
    per_vm_hour: Number(f.vm).toFixed(6),
    notes: f.notes.trim(),
    platform_wide: f.platformWide,
  };
}

/** The card in force at `at` for one scope (tenant id, or null for the platform default). */
export function inForce(cards: RateCard[], scope: string | null, at = new Date()): RateCard | null {
  return cards
    .filter(c => c.tenant_id === scope && Date.parse(c.effective_from) <= at.getTime())
    .sort((a, b) => b.effective_from.localeCompare(a.effective_from))[0] ?? null;
}

export interface Helper {
  hardware: number;     // purchase cost
  years: number;        // paid off over
  monthly: number;      // power, cooling, support per month
  use: number;          // expected average use, %
  vcpu: number;         // capacity
  ramGb: number;
  diskTb: number;
  shareCpu: number;     // % of the cost carried by CPU
  shareRam: number;     // % by RAM; disk carries the rest
}

/** Rates that recover the hardware's cost at the expected use. A helper: it stores nothing.
 *  Disk is paid all the time a range exists, so its rate is not raised for idle capacity. */
export function suggestRates(h: Helper): { perHour: number; vcpu: number; ram: number; diskMonth: number } | null {
  if (h.years <= 0 || h.use <= 0 || h.vcpu <= 0 || h.ramGb <= 0 || h.diskTb <= 0) return null;
  const shareDisk = Math.max(0, 100 - h.shareCpu - h.shareRam);
  const cost = (h.hardware / (h.years * 12) + h.monthly) / HOURS_PER_MONTH;
  const perHour = cost / (h.use / 100);
  return {
    perHour,
    vcpu: (perHour * h.shareCpu) / 100 / h.vcpu,
    ram: (perHour * h.shareRam) / 100 / h.ramGb,
    diskMonth: ((cost * shareDisk) / 100 / (h.diskTb * 1024)) * HOURS_PER_MONTH,
  };
}
