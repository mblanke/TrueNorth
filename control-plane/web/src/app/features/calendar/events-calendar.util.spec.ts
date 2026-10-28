import type { Booking, Timeline } from '@core/services/scheduler-api.service';
import type { MaintenanceWindow } from '@core/services/maintenance-windows-api.service';
import {
  ALL_ON, HOUR_PX, KIND_STYLE, MIN_BLOCK_PX, blockClasses, chipsFor, dayBlocks, dayLoad, hourWindow, inMaintenance,
  kindOf, laneStyle, legendCounts, loadBar, loadLevel, loadText, maintenanceBands, movedTimes, toPx,
  visibleBookings, windowsInPeriod,
} from './events-calendar.util';

const DAY = new Date(2030, 2, 4); // Monday 4 March 2030, local
const at = (h: number, m = 0, day = DAY) => new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m).toISOString();

function booking(p: Partial<Booking> = {}): Booking {
  return {
    id: 'b', name: 'Session', state: 'scheduled', tenant_id: 't', range_id: null, template_id: null, description: null,
    start_time: at(9), end_time: at(12), vm_count: 4, vcpu_total: 10, ram_mb_total: 1000, disk_gb_total: 10,
    created_at: '', updated_at: '', ...p,
  } as Booking;
}

function win(p: Partial<MaintenanceWindow> = {}): MaintenanceWindow {
  return {
    id: 'w', platform_wide: true, reason: 'Patching', starts_at: at(13), ends_at: at(15), scope: 'all', range_ids: [],
    tags: [], active: false, can_edit: false, ...p,
  } as MaintenanceWindow;
}

describe('events-calendar.util', () => {
  describe('kind and colour', () => {
    it('a collective session is an exercise, orange with a group icon', () => {
      const b = booking({ class_id: 'c', range_mode: 'collective' });
      expect(kindOf(b)).toBe('exercise');
      expect(KIND_STYLE.exercise.icon).toBe('groups');
      expect(KIND_STYLE.exercise.colour).toBe('#EF6820');
    });

    it('an individual session is a class, blue with a single-person icon', () => {
      expect(kindOf(booking({ class_id: 'c', range_mode: 'individual' }))).toBe('class');
      expect(KIND_STYLE.class.icon).toBe('person');
      expect(KIND_STYLE.class.colour).toBe('#1570EF');
    });

    it('outside a class, a booking that runs an exercise or scenario is an exercise, else a class', () => {
      expect(kindOf(booking({ exercise_id: 'x' }))).toBe('exercise');
      expect(kindOf(booking({ scenario_id: 's' }))).toBe('exercise');
      expect(kindOf(booking({}))).toBe('class');
    });

    it('marks pending, draft and rejected bookings on the block', () => {
      expect(blockClasses(booking({ state: 'pending_approval', range_mode: 'collective' }))).toBe('ev exercise pending');
      expect(blockClasses(booking({ state: 'draft' }))).toBe('ev class draft');
      expect(blockClasses(booking({ state: 'rejected' }))).toContain('rejected');
      expect(blockClasses(booking({ state: 'scheduled' }))).toBe('ev class');
    });
  });

  describe('filters', () => {
    const cls = booking({ id: 'c', range_mode: 'individual' });
    const ex = booking({ id: 'x', range_mode: 'collective' });
    const pend = booking({ id: 'p', range_mode: 'individual', state: 'pending_approval' });
    const gone = booking({ id: 'g', state: 'cancelled' });

    it('never shows cancelled bookings', () => {
      expect(visibleBookings([cls, gone], ALL_ON).map(b => b.id)).toEqual(['c']);
    });

    it('hides a kind that is switched off', () => {
      expect(visibleBookings([cls, ex, pend], { ...ALL_ON, exercise: false }).map(b => b.id)).toEqual(['c', 'p']);
      expect(visibleBookings([cls, ex, pend], { ...ALL_ON, class: false }).map(b => b.id)).toEqual(['x']);
    });

    it('hides bookings awaiting approval when that filter is off', () => {
      expect(visibleBookings([cls, ex, pend], { ...ALL_ON, pending: false }).map(b => b.id)).toEqual(['c', 'x']);
    });

    it('counts each legend entry', () => {
      expect(legendCounts([cls, ex, pend, gone], [win()])).toEqual({ class: 2, exercise: 1, pending: 1, maintenance: 1 });
    });
  });

  describe('week layout', () => {
    it('runs 08:00 to 17:00 by default', () => {
      expect(hourWindow([booking()], [DAY])).toEqual({ lo: 8, hi: 17 });
    });

    it('widens to whole hours for a booking outside the working day', () => {
      expect(hourWindow([booking({ start_time: at(6, 30), end_time: at(18, 15) })], [DAY])).toEqual({ lo: 6, hi: 19 });
    });

    it('widens to fit a maintenance window outside the working day (review M1)', () => {
      expect(hourWindow([], [DAY], [win({ starts_at: at(20), ends_at: at(23) })])).toEqual({ lo: 8, hi: 23 });
      const [band] = maintenanceBands([win({ starts_at: at(20), ends_at: at(23) })], DAY, 8, 23);
      expect(band.height).toBe(3 * HOUR_PX);
    });

    it('ignores bookings on days not shown', () => {
      const tomorrow = new Date(2030, 2, 5);
      expect(hourWindow([booking({ start_time: at(5, 0, tomorrow), end_time: at(20, 0, tomorrow) })], [DAY]))
        .toEqual({ lo: 8, hi: 17 });
    });

    it('maps a time to pixels from the top of the grid', () => {
      expect(toPx(new Date(at(8)), DAY, 8)).toBe(0);
      expect(toPx(new Date(at(10, 30)), DAY, 8)).toBe(2.5 * HOUR_PX);
      expect(toPx(new Date(at(9)), DAY, 8, 40)).toBe(40);
    });

    it('places a booking by its start and length', () => {
      const [b] = dayBlocks([booking({ start_time: at(9), end_time: at(12) })], DAY, 8, 17);
      expect(b.top).toBe(HOUR_PX);
      expect(b.height).toBe(3 * HOUR_PX);
      expect([b.lane, b.lanes]).toEqual([0, 1]);
    });

    it('puts overlapping bookings side by side, and a later one back in the first lane', () => {
      const blocks = dayBlocks([
        booking({ id: 'a', start_time: at(9), end_time: at(12) }),
        booking({ id: 'b', start_time: at(10), end_time: at(11) }),
        booking({ id: 'c', start_time: at(13), end_time: at(14) }),
      ], DAY, 8, 17);
      const lane = (id: string) => blocks.find(x => x.booking.id === id)!;
      expect(lane('a').lane).toBe(0);
      expect(lane('b').lane).toBe(1);
      expect(lane('c').lane).toBe(0);
      expect(lane('a').lanes).toBe(2);
      expect(laneStyle(1, 2)).toEqual({ left: '50%', width: '50%' });
    });

    it('clips a booking to the grid and to its day, with a minimum height', () => {
      const prev = new Date(2030, 2, 3);
      const [overnight] = dayBlocks([booking({ start_time: at(20, 0, prev), end_time: at(10) })], DAY, 8, 17);
      expect(overnight.top).toBe(0);
      expect(overnight.height).toBe(2 * HOUR_PX);
      const [short] = dayBlocks([booking({ start_time: at(9), end_time: at(9, 5) })], DAY, 8, 17);
      expect(short.height).toBe(MIN_BLOCK_PX);
      expect(dayBlocks([booking({ start_time: at(18), end_time: at(19) })], DAY, 8, 17)).toEqual([]);
    });
  });

  describe('maintenance', () => {
    it('draws a window as a band clipped to the grid', () => {
      const [band] = maintenanceBands([win({ starts_at: at(16), ends_at: at(23) })], DAY, 8, 17);
      expect(band.top).toBe(8 * HOUR_PX);
      expect(band.height).toBe(HOUR_PX);
    });

    it('marks the month cells a window touches', () => {
      const w = win({ starts_at: at(22), ends_at: at(2, 0, new Date(2030, 2, 6)) });
      expect(inMaintenance([w], DAY).length).toBe(1);
      expect(inMaintenance([w], new Date(2030, 2, 5)).length).toBe(1);
      expect(inMaintenance([w], new Date(2030, 2, 7)).length).toBe(0);
    });
  });

  describe('moving a booking (review H2): new start, same length', () => {
    it('keeps a multi-day booking whole: Mon 09:00 to Wed 17:00 moved to Tue ends Thu 17:00', () => {
      const b = booking({ start_time: at(9), end_time: at(17, 0, new Date(2030, 2, 6)) });
      const { start, end } = movedTimes(b, '2030-03-05', '09:00');
      expect(start.getTime()).toBe(new Date(2030, 2, 5, 9).getTime());
      expect(end.getTime()).toBe(new Date(2030, 2, 7, 17).getTime());
    });

    it('carries a booking across midnight', () => {
      const b = booking({ start_time: at(22), end_time: at(2, 0, new Date(2030, 2, 5)) });
      const { start, end } = movedTimes(b, '2030-03-08', '23:00');
      expect(start.getTime()).toBe(new Date(2030, 2, 8, 23).getTime());
      expect(end.getTime()).toBe(new Date(2030, 2, 9, 3).getTime());
    });
  });

  describe('maintenance in the period (review L3)', () => {
    it('keeps only the windows overlapping the shown days', () => {
      const inside = win({ id: 'in', starts_at: at(13), ends_at: at(15) });
      const before = win({ id: 'old', starts_at: at(1, 0, new Date(2030, 1, 1)), ends_at: at(2, 0, new Date(2030, 1, 1)) });
      expect(windowsInPeriod([inside, before], DAY, new Date(2030, 2, 11)).map(w => w.id)).toEqual(['in']);
    });
  });

  describe('month chips', () => {
    it('lists the day\'s bookings earliest first', () => {
      const chips = chipsFor([
        booking({ id: 'late', start_time: at(14), end_time: at(15) }),
        booking({ id: 'early', start_time: at(8), end_time: at(9) }),
        booking({ id: 'other', start_time: at(8, 0, new Date(2030, 2, 9)), end_time: at(9, 0, new Date(2030, 2, 9)) }),
      ], DAY);
      expect(chips.map(b => b.id)).toEqual(['early', 'late']);
    });
  });

  describe('capacity', () => {
    const TL: Timeline = {
      cluster_vcpu: 100, cluster_ram_mb: 100_000, cluster_disk_gb: 1000, supply_source: 'env',
      resolution_minutes: 60, lead_minutes: 30, grace_minutes: 15,
      buckets: [
        { time: at(9), vcpu_committed: 40, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 },
        { time: at(10), vcpu_committed: 60, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 },
        { time: at(14), vcpu_committed: 10, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 },
        { time: at(10, 0, new Date(2030, 2, 5)), vcpu_committed: 99, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 },
      ],
    } as Timeline;

    it('is the day\'s peak committed load', () => {
      expect(dayLoad(TL, [], DAY)).toEqual({ load: 0.6, withPending: 0.6 });
    });

    it('adds pending bookings, build and teardown included, to show the load if approved', () => {
      // 13:00-14:00 holds 12:30-14:15 with lead and grace: only the 14:00 slot (10 committed) is busy, 10 + 30 < 60.
      const pend = booking({ state: 'pending_approval', start_time: at(13), end_time: at(14), vcpu_total: 30 });
      expect(dayLoad(TL, [pend], DAY)).toEqual({ load: 0.6, withPending: 0.6 });
      const big = booking({ state: 'pending_approval', start_time: at(10), end_time: at(11), vcpu_total: 30 });
      expect(dayLoad(TL, [big], DAY).withPending).toBeCloseTo(0.9);
    });

    it('takes the tightest resource', () => {
      const tl = { ...TL, buckets: [{ time: at(9), vcpu_committed: 10, ram_mb_committed: 80_000, disk_gb_committed: 0, event_count: 1 }] };
      expect(dayLoad(tl as Timeline, [], DAY).load).toBeCloseTo(0.8);
    });

    it('is nothing without a timeline', () => {
      expect(dayLoad(null, [booking({ state: 'pending_approval' })], DAY)).toEqual({ load: 0, withPending: 0 });
    });

    it('colours by the uncapped load, so over capacity shows as high (review L2)', () => {
      expect(loadLevel({ load: 1.4, withPending: 1.4 })).toBe('high');
      expect(loadLevel({ load: 0.7, withPending: 1.2 })).toBe('mid');
      expect(loadLevel({ load: 0.3, withPending: 0.3 })).toBe('ok');
      expect(loadLevel({ load: 0, withPending: 0 })).toBe('none');
    });

    it('draws the pending extra after the committed part, capped at the track', () => {
      expect(loadBar({ load: 0.6, withPending: 0.9 })).toEqual({ used: 60, extra: 30 });
      expect(loadBar({ load: 0.8, withPending: 1.4 })).toEqual({ used: 80, extra: 20 });
      expect(loadText({ load: 0.6, withPending: 0.9 })).toBe('60% · 90% if approved');
      expect(loadText({ load: 0.6, withPending: 0.6 })).toBe('60%');
    });
  });
});
