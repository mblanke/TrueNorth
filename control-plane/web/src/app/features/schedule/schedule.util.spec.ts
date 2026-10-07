import type { Booking } from '@core/services/scheduler-api.service';
import { addDays, layoutLanes, level, localizeUtcWindows, peakLoad, previewConflicts, slotLoads, weekStart } from './schedule.util';

function booking(p: Partial<Booking>): Booking {
  return {
    id: 'b', name: 'x', state: 'scheduled', tenant_id: 't', range_id: null, template_id: null, description: null,
    start_time: '', end_time: '', vm_count: 0, vcpu_total: 0, ram_mb_total: 0, disk_gb_total: 0,
    created_at: '', updated_at: '', ...p,
  } as Booking;
}

describe('schedule.util', () => {
  it('weekStart is the Monday at midnight, local', () => {
    const sunday = new Date(2026, 9, 18, 15, 30);
    const monday = weekStart(sunday);
    expect(monday.getDay()).toBe(1);
    expect(monday.getDate()).toBe(12);
    expect(monday.getHours()).toBe(0);
    expect(weekStart(monday).getTime()).toBe(monday.getTime());
  });

  it('lays overlapping sessions side by side and reuses free lanes', () => {
    const out = layoutLanes([
      { start: 9, end: 12, id: 'a' }, { start: 10, end: 12, id: 'b' }, { start: 13, end: 16, id: 'c' },
    ]);
    const lane = (id: string) => out.find(o => o.item.id === id)!.lane;
    expect(out[0].lanes).toBe(2);
    expect(lane('a')).toBe(0);
    expect(lane('b')).toBe(1);
    expect(lane('c')).toBe(0); // a has ended by 13:00
  });

  it('levels follow the legend: under 60% green, to 100% amber, over red', () => {
    expect(level(0)).toBe('none');
    expect(level(0.59)).toBe('ok');
    expect(level(0.6)).toBe('mid');
    expect(level(1)).toBe('mid');
    expect(level(1.01)).toBe('high');
  });

  it('loads are the tightest resource, and peaks look only inside the window', () => {
    const t0 = new Date(2026, 9, 12, 9);
    const loads = slotLoads({
      buckets: [
        { time: t0.toISOString(), vcpu_committed: 10, ram_mb_committed: 900, disk_gb_committed: 1, event_count: 1 },
        { time: new Date(t0.getTime() + 900_000).toISOString(), vcpu_committed: 90, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 },
      ],
      cluster_vcpu: 100, cluster_ram_mb: 1000, cluster_disk_gb: 100,
      supply_source: 'env', resolution_minutes: 15, lead_minutes: 30, grace_minutes: 15,
    });
    expect(loads[0].load).toBeCloseTo(0.9); // RAM
    expect(peakLoad(loads, t0, new Date(t0.getTime() + 900_000))).toBeCloseTo(0.9);
    expect(peakLoad(loads, new Date(t0.getTime() + 3_600_000), addDays(t0, 1))).toBe(0);
  });

  it('previews double bookings the way the server refuses them', () => {
    const day = new Date(2026, 9, 12);
    const at = (h: number, m = 0) => new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
    const morning = booking({ id: 'm', range_id: 'r1', instructor_id: 'i1', start_time: at(9).toISOString(), end_time: at(12).toISOString() });
    const gone = booking({ id: 'g', range_id: 'r1', state: 'cancelled', start_time: at(13).toISOString(), end_time: at(14).toISOString() });
    const all = [morning, gone];
    // Range: needs lead + grace (45 min) after 12:00.
    expect(previewConflicts({ start: at(12, 30), end: at(14), rangeId: 'r1' }, all, 30, 15).map(b => b.id)).toEqual(['m']);
    expect(previewConflicts({ start: at(12, 45), end: at(14), rangeId: 'r1' }, all, 30, 15)).toEqual([]);
    // Instructor: only the session itself.
    expect(previewConflicts({ start: at(12), end: at(14), instructorId: 'i1' }, all, 30, 15)).toEqual([]);
    expect(previewConflicts({ start: at(11), end: at(14), instructorId: 'i1' }, all, 30, 15).map(b => b.id)).toEqual(['m']);
    // Editing a booking never conflicts with itself.
    expect(previewConflicts({ id: 'm', start: at(9), end: at(12), rangeId: 'r1' }, all, 30, 15)).toEqual([]);
  });

  it('shows the API’s UTC windows in local time', () => {
    const out = localizeUtcWindows('vCPU: need 69, 41 free 2026-10-07 16:30–20:15 UTC');
    const a = new Date('2026-10-07T16:30:00Z'), b = new Date('2026-10-07T20:15:00Z');
    const two = (n: number) => String(n).padStart(2, '0');
    expect(out).toContain(`${two(a.getHours())}:${two(a.getMinutes())}–${two(b.getHours())}:${two(b.getMinutes())}`);
    expect(out).not.toContain('UTC');
    expect(out.startsWith('vCPU: need 69, 41 free ')).toBeTrue();
    expect(localizeUtcWindows('no window here')).toBe('no window here');
  });
});
