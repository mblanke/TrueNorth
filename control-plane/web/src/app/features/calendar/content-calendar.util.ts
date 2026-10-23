/**
 * Pure helpers of the calendar's Content view (ADR 0014 decisions 22-24): the grid's days,
 * the heading, the clock badge's words, colours and labels. Dates are the browser's own
 * (local) calendar days; the API speaks instants.
 */
import type { ContentItem, ContentItemBrief, ContentTheme } from '@core/services/content-review-api.service';

export type CalendarView = 'week' | 'month';

/** Shown for an item with no theme (the mockup's Platform grey). */
export const NO_THEME_COLOUR = '#6B7280';

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
  'November', 'December'];

const KINDS: Record<string, string> = {
  course: 'Course',
  module: 'Module',
  lab: 'Lab',
  scenario: 'Scenario',
  range_template: 'Range template',
  golden_template: 'Golden template',
};

/** Midnight (local) of the Monday on or before `d`. */
export function startOfWeek(d: Date): Date {
  const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return out;
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** The days the grid shows: one week, or the six weeks around the anchor's month. */
export function gridDays(anchor: Date, view: CalendarView): Date[] {
  const first = view === 'week' ? startOfWeek(anchor) : startOfWeek(new Date(anchor.getFullYear(), anchor.getMonth(), 1));
  return Array.from({ length: view === 'week' ? 7 : 42 }, (_, i) => addDays(first, i));
}

/** The anchor one period earlier (-1) or later (+1). */
export function shift(anchor: Date, view: CalendarView, dir: -1 | 1): Date {
  if (view === 'week') return addDays(anchor, 7 * dir);
  return new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1);
}

/** "November 2026", or for a week "19 – 25 October" (across months: "27 October – 2 November"). */
export function heading(anchor: Date, view: CalendarView): string {
  if (view === 'month') return `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
  const from = startOfWeek(anchor);
  const to = addDays(from, 6);
  if (from.getMonth() === to.getMonth()) return `${from.getDate()} – ${to.getDate()} ${MONTHS[to.getMonth()]}`;
  return `${from.getDate()} ${MONTHS[from.getMonth()]} – ${to.getDate()} ${MONTHS[to.getMonth()]}`;
}

/** A local calendar day as YYYY-MM-DD. */
export function dayKey(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The clock badge's words: why it is stale, or how long until the review is due. */
export function clockText(item: Pick<ContentItem, 'stale' | 'stale_reasons' | 'clock'>): string {
  if (item.stale) return `Stale: ${item.stale_reasons?.[0]?.text ?? 'review due'}`;
  const days = item.clock.days;
  if (days <= 0) return 'Review due today';
  return days === 1 ? 'Review due tomorrow' : `Review due in ${days} days`;
}

/** The item's colour: its first theme's, else grey. */
export function themeOf(item: Pick<ContentItem, 'theme_ids'>, themes: Map<string, ContentTheme>): ContentTheme | null {
  for (const id of item.theme_ids ?? []) {
    const t = themes.get(id);
    if (t) return t;
  }
  return null;
}

export function kindLabel(itemType: string): string {
  return KINDS[itemType] ?? itemType;
}

/** "None", the titles, or the first three and how many more. */
export function listText(items: readonly ContentItemBrief[]): string {
  if (!items.length) return 'None';
  const titles = items.map(i => i.title);
  return titles.length <= 3 ? titles.join(', ') : `${titles.slice(0, 3).join(', ')} and ${titles.length - 3} more`;
}
