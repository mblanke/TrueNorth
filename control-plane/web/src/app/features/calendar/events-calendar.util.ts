/**
 * Pure helpers of the calendar's Events view (ADR 0014 decision 21, issue #285): what kind
 * of booking a session is and how it is drawn, where it sits on the week's timed grid,
 * which bookings the legend filters let through, maintenance bands, and each day's
 * capacity with and without the pending bookings. The server is the authority on fit;
 * these only shape what the page shows.
 */
import type { Booking, Timeline } from '@core/services/scheduler-api.service';
import type { MaintenanceWindow } from '@core/services/maintenance-windows-api.service';
import { addDays, hourOf, layoutLanes, sameDay } from '../schedule/schedule.util';

/** A class session with a range per Student, or an exercise on one shared range (ADR 0014 §9). */
export type EventKind = 'class' | 'exercise';

export interface KindStyle { label: string; colour: string; icon: string; hint: string }

/** Blue = class (individual ranges), orange = exercise (collective, shared range). */
export const KIND_STYLE: Record<EventKind, KindStyle> = {
  class: { label: 'Class', colour: '#1570EF', icon: 'person', hint: 'A range for each Student' },
  exercise: { label: 'Exercise', colour: '#EF6820', icon: 'groups', hint: 'One shared range' },
};

/** The timed grid's working day, widened when a booking falls outside it. */
export const DAY_START = 8;
export const DAY_END = 17;
export const HOUR_PX = 56;
/** A short booking still gets room for its name. */
export const MIN_BLOCK_PX = 22;

/**
 * A class session is what its mode says. A booking outside a class is an exercise when
 * it runs one (an exercise or a scenario), otherwise a class.
 */
export function kindOf(b: Pick<Booking, 'range_mode' | 'exercise_id' | 'scenario_id'>): EventKind {
  if (b.range_mode === 'collective') return 'exercise';
  if (b.range_mode === 'individual') return 'class';
  return b.exercise_id || b.scenario_id ? 'exercise' : 'class';
}

export function isPending(b: Pick<Booking, 'state'>): boolean {
  return b.state === 'pending_approval';
}

/** The classes on the event block: kind, and pending/draft/rejected/past styling. */
export function blockClasses(b: Pick<Booking, 'state' | 'range_mode' | 'exercise_id' | 'scenario_id'>): string {
  const out = ['ev', kindOf(b)];
  if (isPending(b)) out.push('pending');
  if (b.state === 'draft') out.push('draft');
  if (b.state === 'rejected') out.push('rejected');
  if (b.state === 'completed') out.push('done');
  return out.join(' ');
}

export interface Filters { class: boolean; exercise: boolean; pending: boolean; maintenance: boolean }
export const ALL_ON: Filters = { class: true, exercise: true, pending: true, maintenance: true };

/** What the legend lets through. Cancelled bookings are never drawn. */
export function visibleBookings(bookings: readonly Booking[], f: Filters): Booking[] {
  return bookings.filter(b => b.state !== 'cancelled' && f[kindOf(b)] && (f.pending || !isPending(b)));
}

/** The legend's counts, over the bookings in the period (cancelled ones excluded). */
export function legendCounts(bookings: readonly Booking[], windows: readonly MaintenanceWindow[]): Record<keyof Filters, number> {
  const live = bookings.filter(b => b.state !== 'cancelled');
  return {
    class: live.filter(b => kindOf(b) === 'class').length,
    exercise: live.filter(b => kindOf(b) === 'exercise').length,
    pending: live.filter(isPending).length,
    maintenance: windows.length,
  };
}

function overlapsDay(start: Date, end: Date, day: Date): boolean {
  const from = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  return start < addDays(from, 1) && end > from;
}

/** The grid's hours: 08:00-17:00, widened to whole hours to fit any booking shown in `days`. */
export function hourWindow(bookings: readonly Booking[], days: readonly Date[]): { lo: number; hi: number } {
  let lo = DAY_START, hi = DAY_END;
  for (const b of bookings) {
    const s = new Date(b.start_time), e = new Date(b.end_time);
    for (const d of days) {
      if (!overlapsDay(s, e, d)) continue;
      lo = Math.min(lo, Math.max(0, Math.floor(hourOf(s, d))));
      hi = Math.max(hi, Math.min(24, Math.ceil(hourOf(e, d))));
    }
  }
  return { lo, hi };
}

/** Pixels from the top of the grid (which starts at `lo` o'clock) to `t` on `day`. */
export function toPx(t: Date, day: Date, lo: number, hourPx = HOUR_PX): number {
  return (hourOf(t, day) - lo) * hourPx;
}

export interface Block { booking: Booking; top: number; height: number; lane: number; lanes: number }

/**
 * The day's bookings as blocks on the timed grid, clipped to [lo, hi), overlapping ones
 * side by side in the fewest lanes.
 */
export function dayBlocks(bookings: readonly Booking[], day: Date, lo: number, hi: number, hourPx = HOUR_PX): Block[] {
  const spans = bookings
    .filter(b => overlapsDay(new Date(b.start_time), new Date(b.end_time), day))
    .map(b => ({
      booking: b,
      start: Math.max(lo, hourOf(new Date(b.start_time), day)),
      end: Math.min(hi, hourOf(new Date(b.end_time), day)),
    }))
    .filter(s => s.end > s.start);
  return layoutLanes(spans).map(({ item, lane, lanes }) => ({
    booking: item.booking,
    top: (item.start - lo) * hourPx,
    height: Math.max(MIN_BLOCK_PX, (item.end - item.start) * hourPx),
    lane,
    lanes,
  }));
}

/** A block's horizontal place in its day column, as CSS percentages. */
export function laneStyle(lane: number, lanes: number): { left: string; width: string } {
  const w = 100 / lanes;
  return { left: `${lane * w}%`, width: `${w}%` };
}

export interface Band { window: MaintenanceWindow; top: number; height: number }

/** Maintenance windows on the day, as grey bands clipped to the grid's hours. */
export function maintenanceBands(
  windows: readonly MaintenanceWindow[], day: Date, lo: number, hi: number, hourPx = HOUR_PX,
): Band[] {
  return windows
    .filter(w => overlapsDay(new Date(w.starts_at), new Date(w.ends_at), day))
    .map(w => {
      const from = Math.max(lo, hourOf(new Date(w.starts_at), day));
      const to = Math.min(hi, hourOf(new Date(w.ends_at), day));
      return { window: w, top: (from - lo) * hourPx, height: (to - from) * hourPx };
    })
    .filter(b => b.height > 0);
}

/** Whether a maintenance window touches the calendar day at all (the month view). */
export function inMaintenance(windows: readonly MaintenanceWindow[], day: Date): MaintenanceWindow[] {
  return windows.filter(w => overlapsDay(new Date(w.starts_at), new Date(w.ends_at), day));
}

/** The month cell's chips: bookings running on the day, earliest first. */
export function chipsFor(bookings: readonly Booking[], day: Date): Booking[] {
  return bookings
    .filter(b => overlapsDay(new Date(b.start_time), new Date(b.end_time), day))
    .sort((a, b) => a.start_time.localeCompare(b.start_time));
}

export interface DayLoad { load: number; withPending: number }

/**
 * The day's peak load (fraction of the tightest resource, build and teardown included) as
 * committed, and as it would be if every pending booking were approved too (ADR 0014 §15:
 * pending bookings hold nothing, so the timeline leaves them out).
 */
export function dayLoad(t: Timeline | null, bookings: readonly Booking[], day: Date): DayLoad {
  if (!t) return { load: 0, withPending: 0 };
  const res = t.resolution_minutes * 60_000;
  const lead = t.lead_minutes * 60_000, grace = t.grace_minutes * 60_000;
  const pending = bookings
    .filter(isPending)
    .map(b => ({ b, from: new Date(b.start_time).getTime() - lead, to: new Date(b.end_time).getTime() + grace }));
  const frac = (vcpu: number, ram: number, disk: number) => Math.max(
    t.cluster_vcpu ? vcpu / t.cluster_vcpu : 0,
    t.cluster_ram_mb ? ram / t.cluster_ram_mb : 0,
    t.cluster_disk_gb ? disk / t.cluster_disk_gb : 0,
  );
  let load = 0, withPending = 0;
  for (const bucket of t.buckets) {
    const at = new Date(bucket.time);
    if (!sameDay(at, day)) continue;
    const s = at.getTime(), e = s + res;
    let v = bucket.vcpu_committed, r = bucket.ram_mb_committed, d = bucket.disk_gb_committed;
    load = Math.max(load, frac(v, r, d));
    for (const p of pending) {
      if (p.from < e && p.to > s) { v += p.b.vcpu_total; r += p.b.ram_mb_total; d += p.b.disk_gb_total; }
    }
    withPending = Math.max(withPending, frac(v, r, d));
  }
  return { load, withPending };
}

/** Bar widths (percent of the track) for the committed load and the pending extra. */
export function loadBar(l: DayLoad): { used: number; extra: number } {
  const used = Math.min(100, Math.round(l.load * 100));
  return { used, extra: Math.max(0, Math.min(100, Math.round(l.withPending * 100)) - used) };
}

/** "62%", or with pending bookings "62% · 80% if approved". */
export function loadText(l: DayLoad): string {
  const now = Math.round(l.load * 100), next = Math.round(l.withPending * 100);
  return next > now ? `${now}% · ${next}% if approved` : `${now}%`;
}
