import { MeslEvent } from '@core/services/api.service';
import { DELIVERY_METHODS, MESL_STATUSES, groupByPhase } from './mesl-board.component';

function event(overrides: Partial<MeslEvent>): MeslEvent {
  return {
    id: overrides.id ?? 'e1',
    serial: overrides.serial ?? 1,
    phase: overrides.phase ?? '',
    scenario_time: overrides.scenario_time ?? 'D1 0800',
    title: overrides.title ?? 'Serial',
    description: '',
    objective_ref: '',
    attack_technique: '',
    delivery_method: overrides.delivery_method ?? 'cyber',
    from_cell: '',
    to_participant: '',
    expected_action: '',
    moe: '',
    status: overrides.status ?? 'planned',
  };
}

describe('MESL board grouping', () => {
  it('groups serials by phase in first-seen order', () => {
    const groups = groupByPhase([
      event({ id: 'a', serial: 1, phase: 'Shaping' }),
      event({ id: 'b', serial: 2, phase: 'Contact' }),
      event({ id: 'c', serial: 3, phase: 'Shaping' }),
    ]);
    expect(groups.map(g => g.phase)).toEqual(['Shaping', 'Contact']);
    expect(groups[0].events.map(e => e.id)).toEqual(['a', 'c']);
  });

  it('orders serials numerically inside a phase, whatever order they arrive in', () => {
    const groups = groupByPhase([
      event({ id: 'later', serial: 12, phase: 'P' }),
      event({ id: 'earlier', serial: 3, phase: 'P' }),
    ]);
    // Serial 3 before 12 — a string sort would have put 12 first.
    expect(groups[0].events.map(e => e.serial)).toEqual([3, 12]);
  });

  it('buckets blank phases rather than dropping those serials', () => {
    const groups = groupByPhase([event({ id: 'x', phase: '   ' })]);
    expect(groups.length).toBe(1);
    expect(groups[0].phase).toBe('Unphased');
    expect(groups[0].events[0].id).toBe('x');
  });

  it('returns nothing for an empty MESL', () => {
    expect(groupByPhase([])).toEqual([]);
  });

  it('does not mutate the input array order', () => {
    const input = [event({ id: 'b', serial: 2 }), event({ id: 'a', serial: 1 })];
    groupByPhase(input);
    expect(input.map(e => e.id)).toEqual(['b', 'a']);
  });
});

describe('MESL vocabularies', () => {
  it('mirrors the vocabularies the API enforces', () => {
    // These must match app/routers/exercises_collective.py or the board can
    // author values the PATCH endpoint rejects with a 422.
    expect([...DELIVERY_METHODS]).toEqual(['cyber', 'white_cell', 'email', 'radio', 'physical', 'opfor']);
    expect([...MESL_STATUSES]).toEqual(['planned', 'staged', 'delivered', 'responded', 'skipped']);
  });
});
