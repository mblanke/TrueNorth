// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
import type { RateCard, UsageInterval } from '@core/services/usage-api.service';
import {
  RateForm, inForce, isBucket, isoToLocal, period, rangeGroups, rateCardBody, suggestRates, toHourly,
} from './usage-cost.util';

const FIG = {
  vcpu_hours: 0, ram_gb_hours: 0, disk_gb_hours: 0, vm_hours: 0, cost_vcpu: 0, cost_ram: 0, cost_disk: 0, cost_vm: 0,
  cost_total: 0, currency: 'CAD', unpriced_hours: 0,
};
const iv = (over: Partial<UsageInterval>): UsageInterval => ({
  ...FIG, source: 'class_seat', source_id: 's1', range_id: 'r1', course_id: 'c1', class_id: 'k1',
  start: '2026-11-02T09:00:00Z', end: '2026-11-02T16:00:00Z', vcpu: 10, ram_gb: 20, disk_gb: 300, vms: 5,
  approximate: false, open: false, compute: true, range_name: 'seat-fox', course_name: 'Network Defence', class_name: 'Cohort 3',
  ...over,
});

describe('usage cost helpers', () => {
  it('runs a period from the start of the month (local) to now', () => {
    const now = new Date(2026, 10, 28, 10, 0);
    expect(period('month', now)).toEqual({ from: new Date(2026, 10, 1).toISOString(), to: now.toISOString() });
    expect(period('previous', now)).toEqual({ from: new Date(2026, 9, 1).toISOString(), to: new Date(2026, 10, 1).toISOString() });
    // January's previous month is last year's December.
    expect(period('previous', new Date(2027, 0, 5)).from).toBe(new Date(2026, 11, 1).toISOString());
  });

  it('knows a bucket row from a named one', () => {
    expect(isBucket({ key: 'unattributed:t1' })).toBeTrue();
    expect(isBucket({ key: '6f1c9d1e-0000-4000-8000-000000000000' })).toBeFalse();
  });

  it('sums one range into running, stopped and approximate time', () => {
    const groups = rangeGroups([
      iv({ start: '2026-11-02T16:00:00Z', end: '2026-11-03T09:00:00Z', compute: false, cost_total: 0.5 }),
      iv({ cost_total: 2 }),
      iv({ start: '2026-11-01T09:00:00Z', end: '2026-11-02T09:00:00Z', approximate: true, cost_total: 6 }),
      iv({ range_id: 'r2', source: 'lab', range_name: null, class_name: null, start: '2026-11-05T10:00:00Z', end: '2026-11-05T12:00:00Z', cost_total: 1, vms: 1 }),
    ]);
    expect(groups.map(g => g.key)).toEqual(['r1', 'r2']);
    const [seat, lab] = groups;
    expect(seat.title).toBe('Seat range · seat-fox');
    expect(seat.context).toBe('Network Defence · Cohort 3');
    expect(seat.running).toEqual({ hours: 7, cost: 2 });
    expect(seat.stopped).toEqual({ hours: 17, cost: 0.5 });
    expect(seat.approx).toEqual({ hours: 24, cost: 6 });
    expect(seat.total).toBe(8.5);
    expect(seat.start).toBe('2026-11-01T09:00:00Z');
    expect(lab.title).toBe('Self-serve lab');
    expect(lab.size.startsWith('1 VM,')).toBeTrue();
  });

  it('builds a rate card: disk per month stored per hour, the time as an instant', () => {
    const now = new Date('2026-11-28T10:00:00Z');
    const f: RateForm = { effectiveFrom: isoToLocal('2026-12-01T05:00:00Z'), currency: 'CAD', vcpu: '0.018', ram: '0.0045',
      diskMonth: '0.1095', vm: '0', notes: ' storage ', platformWide: true };
    const body = rateCardBody(f, now);
    expect(typeof body).toBe('object');
    const b = body as Exclude<typeof body, string>;
    expect(b.effective_from).toBe('2026-12-01T05:00:00.000Z');
    expect(b.per_gb_disk_hour).toBe(toHourly(0.1095));
    expect(b.per_gb_disk_hour).toBe('0.000150');
    expect(b.per_vcpu_hour).toBe('0.018000');
    expect(b.notes).toBe('storage');
    expect(b.platform_wide).toBeTrue();
  });

  it('refuses a card in the past, a negative rate and a bad currency, in words', () => {
    const now = new Date('2026-11-28T10:00:00Z');
    const f: RateForm = { effectiveFrom: isoToLocal('2026-12-01T00:00:00Z'), currency: 'CAD', vcpu: '1', ram: '1',
      diskMonth: '1', vm: '0', notes: '', platformWide: false };
    expect(rateCardBody({ ...f, effectiveFrom: isoToLocal('2026-11-01T00:00:00Z') }, now)).toContain('never changes');
    expect(rateCardBody({ ...f, vcpu: '-1' }, now)).toContain('0 or more');
    expect(rateCardBody({ ...f, ram: '' }, now)).toContain('0 or more');
    expect(rateCardBody({ ...f, currency: 'cad' }, now)).toContain('3-letter');
    expect(rateCardBody({ ...f, effectiveFrom: '' }, now)).toContain('takes over');
  });

  it('finds the card in force for a scope', () => {
    const card = (id: string, tenant: string | null, from: string) => ({ id, tenant_id: tenant, effective_from: from } as RateCard);
    const cards = [card('a', null, '2026-10-01T00:00:00Z'), card('b', null, '2026-11-15T00:00:00Z'),
      card('c', null, '2026-12-15T00:00:00Z'), card('d', 't1', '2026-11-01T00:00:00Z')];
    const at = new Date('2026-11-28T00:00:00Z');
    expect(inForce(cards, null, at)?.id).toBe('b');
    expect(inForce(cards, 't1', at)?.id).toBe('d');
    expect(inForce(cards, 't2', at)).toBeNull();
  });

  it('works out rates from the hardware, and nothing from an empty capacity', () => {
    const s = suggestRates({ hardware: 1_800_000, years: 5, monthly: 9000, use: 60, vcpu: 3000, ramGb: 24000, diskTb: 600, shareCpu: 50, shareRam: 35 });
    expect(s).not.toBeNull();
    const cost = (1_800_000 / 60 + 9000) / 730;
    expect(s!.perHour).toBeCloseTo(cost / 0.6, 6);
    expect(s!.vcpu).toBeCloseTo((cost / 0.6) * 0.5 / 3000, 9);
    expect(s!.diskMonth).toBeCloseTo(cost * 0.15 / (600 * 1024) * 730, 9);
    expect(suggestRates({ hardware: 1, years: 5, monthly: 0, use: 60, vcpu: 0, ramGb: 1, diskTb: 1, shareCpu: 50, shareRam: 35 })).toBeNull();
  });
});
