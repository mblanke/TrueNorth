import {
  NO_THEME_COLOUR, clockText, dayKey, gridDays, heading, kindLabel, listText, shift, startOfWeek, themeOf,
} from './content-calendar.util';
import type { ContentItemBrief, ContentTheme } from '@core/services/content-review-api.service';

describe('content calendar helpers', () => {
  const oct22 = new Date(2026, 9, 22); // a Thursday

  it('starts weeks on Monday', () => {
    expect(dayKey(startOfWeek(oct22))).toBe('2026-10-19');
    expect(dayKey(startOfWeek(new Date(2026, 9, 25)))).toBe('2026-10-19'); // Sunday
    expect(dayKey(startOfWeek(new Date(2026, 9, 26)))).toBe('2026-10-26'); // Monday
  });

  it('shows one week, or six weeks around the month', () => {
    const week = gridDays(oct22, 'week');
    expect(week.length).toBe(7);
    expect(dayKey(week[0])).toBe('2026-10-19');
    const month = gridDays(new Date(2026, 10, 15), 'month');
    expect(month.length).toBe(42);
    expect(dayKey(month[0])).toBe('2026-10-26'); // the Monday before 1 November
  });

  it('moves a week or a month at a time', () => {
    expect(dayKey(shift(oct22, 'week', 1))).toBe('2026-10-29');
    expect(dayKey(shift(oct22, 'month', -1))).toBe('2026-09-01');
    expect(dayKey(shift(new Date(2026, 11, 31), 'month', 1))).toBe('2027-01-01');
  });

  it('names the period as the mockup does', () => {
    expect(heading(new Date(2026, 10, 3), 'month')).toBe('November 2026');
    expect(heading(oct22, 'week')).toBe('19 – 25 October');
    expect(heading(new Date(2026, 9, 28), 'week')).toBe('26 October – 1 November');
  });

  it('says why an item is stale, or when it is due', () => {
    const base = { stale: false, stale_reasons: [], clock: { state: 'green' as const, days: 12 } };
    expect(clockText(base)).toBe('Review due in 12 days');
    expect(clockText({ ...base, clock: { state: 'amber', days: 1 } })).toBe('Review due tomorrow');
    expect(clockText({ ...base, clock: { state: 'amber', days: 0 } })).toBe('Review due today');
    expect(clockText({
      ...base, stale: true,
      stale_reasons: [{ kind: 'upstream', text: 'Its golden template Ubuntu 24.04 is stale', origin_id: 'g' }],
    })).toBe('Stale: Its golden template Ubuntu 24.04 is stale');
  });

  it('colours an item by its first known theme', () => {
    const blue: ContentTheme = { id: 'b', name: 'Blue team', colour: '#3B5BDB', ordinal: 0, platform_wide: true };
    const themes = new Map([[blue.id, blue]]);
    expect(themeOf({ theme_ids: ['gone', 'b'] }, themes)).toBe(blue);
    expect(themeOf({ theme_ids: [] }, themes)).toBeNull();
    expect(NO_THEME_COLOUR).toBe('#6B7280');
  });

  it('labels kinds and lists', () => {
    expect(kindLabel('golden_template')).toBe('Golden template');
    expect(kindLabel('range_template')).toBe('Range template');
    const b = (title: string) => ({ id: title, item_type: 'course', title, stale: false, retired: false }) as ContentItemBrief;
    expect(listText([])).toBe('None');
    expect(listText([b('A'), b('B')])).toBe('A, B');
    expect(listText([b('A'), b('B'), b('C'), b('D'), b('E')])).toBe('A, B, C and 2 more');
  });
});
