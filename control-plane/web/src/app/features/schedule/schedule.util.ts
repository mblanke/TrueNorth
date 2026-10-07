/**
 * Pure helpers for the Schedule page (ADR 0004 slice 8): week maths, side-by-side
 * lanes, load levels, and the client-side preview of double bookings. The server is
 * the authority; these only shape what the page shows before it asks.
 */
import type { Booking, Timeline } from '@core/services/scheduler-api.service';

export const HOUR_MS = 3_600_000;
export const DAY_MS = 24 * HOUR_MS;

/** Monday 00:00 local of the week containing `d`. */
export function weekStart(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const offset = (x.getDay() + 6) % 7; // Mon = 0
  x.setDate(x.getDate() - offset);
  return x;
}

export function addDays(d: Date, n: number): Date {
  const x = new Date(d);
  x.setDate(x.getDate() + n);
  return x;
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Hours since local midnight of `day`, fractional. */
export function hourOf(t: Date, day: Date): number {
  return (t.getTime() - new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime()) / HOUR_MS;
}

export interface Span { start: number; end: number }
export interface Laned<T> { item: T; lane: number; lanes: number }

/** Side-by-side lanes for overlapping spans: the fewest lanes, earliest first. */
export function layoutLanes<T extends Span>(items: T[]): Laned<T>[] {
  const sorted = [...items].sort((a, b) => a.start - b.start || b.end - a.end);
  const laneEnds: number[] = [];
  const placed = sorted.map(item => {
    let lane = laneEnds.findIndex(end => end <= item.start);
    if (lane < 0) lane = laneEnds.length;
    laneEnds[lane] = item.end;
    return { item, lane };
  });
  const lanes = Math.max(1, laneEnds.length);
  return placed.map(p => ({ ...p, lanes }));
}

export type Level = 'none' | 'ok' | 'mid' | 'high';

/** <60% green, 60-100% amber, over red: the mockup's legend. */
export function level(fraction: number): Level {
  if (fraction <= 0) return 'none';
  if (fraction < 0.6) return 'ok';
  if (fraction <= 1) return 'mid';
  return 'high';
}

/** Fraction of the tightest resource committed in each timeline slot. */
export function slotLoads(t: Timeline): { time: Date; load: number }[] {
  return t.buckets.map(b => ({
    time: new Date(b.time),
    load: Math.max(
      t.cluster_vcpu ? b.vcpu_committed / t.cluster_vcpu : 0,
      t.cluster_ram_mb ? b.ram_mb_committed / t.cluster_ram_mb : 0,
      t.cluster_disk_gb ? b.disk_gb_committed / t.cluster_disk_gb : 0,
    ),
  }));
}

/** Worst slot within [from, to). */
export function peakLoad(loads: { time: Date; load: number }[], from: Date, to: Date): number {
  let peak = 0;
  for (const s of loads) if (s.time >= from && s.time < to && s.load > peak) peak = s.load;
  return peak;
}

export const HOLDING_STATES = ['scheduled', 'provisioning', 'active'];

/**
 * Double bookings the server will refuse (service.conflicts): a range is held for the
 * lead and grace too, an instructor only for the session.
 */
export function previewConflicts(
  proposal: { id?: string; start: Date; end: Date; rangeId?: string | null; instructorId?: string | null },
  bookings: Booking[],
  leadMin: number,
  graceMin: number,
): Booking[] {
  const reach = (leadMin + graceMin) * 60_000;
  return bookings.filter(b => {
    if (b.id === proposal.id || !HOLDING_STATES.includes(b.state)) return false;
    const s = new Date(b.start_time).getTime(), e = new Date(b.end_time).getTime();
    const ps = proposal.start.getTime(), pe = proposal.end.getTime();
    const sameRange = !!proposal.rangeId && b.range_id === proposal.rangeId && s < pe + reach && e > ps - reach;
    const sameInstructor = !!proposal.instructorId && b.instructor_id === proposal.instructorId && s < pe && e > ps;
    return sameRange || sameInstructor;
  });
}

export function gb(mb: number): string {
  return mb % 1024 === 0 || mb >= 10240 ? String(Math.round(mb / 1024)) : (mb / 1024).toFixed(1);
}

export function diskLabel(g: number): string {
  return g >= 1000 ? `${(g / 1024).toFixed(1)} TB` : `${g} GB`;
}

export function hhmm(d: Date): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** `yyyy-mm-dd` for <input type=date>, local. */
export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Local date + `HH:MM` -> Date. */
export function at(date: string, time: string): Date {
  return new Date(`${date}T${time}:00`);
}

/**
 * The API words windows in UTC ("2026-10-07 16:30–20:15 UTC", capacity.window_label).
 * Show them in the viewer's time zone like everything else on the page.
 */
export function localizeUtcWindows(text: string): string {
  return text.replace(
    /(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})–(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2}) UTC/g,
    (_m, d1: string, t1: string, d2: string | undefined, t2: string) => {
      const a = new Date(`${d1}T${t1}:00Z`), b = new Date(`${d2 ?? d1}T${t2}:00Z`);
      const day = (d: Date) => d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
      return sameDay(a, b) ? `${day(a)} ${hhmm(a)}–${hhmm(b)}` : `${day(a)} ${hhmm(a)}–${day(b)} ${hhmm(b)}`;
    },
  );
}
