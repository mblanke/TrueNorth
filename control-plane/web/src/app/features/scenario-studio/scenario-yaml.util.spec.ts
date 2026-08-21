import {
  ScenarioModel,
  emptyScenario,
  fromNormalized,
  normalizeTime,
  slug,
  timeToSeconds,
  toYaml,
  totalPoints,
} from './scenario-yaml.util';

function sample(): ScenarioModel {
  return {
    name: 'Quick Detection Drill',
    version: '1.0',
    description: 'Spot the beacon',
    range_template: 'small-enterprise',
    timeline: [
      { t: '00:00', action: 'dns_spike', params: { domains: 'bad.example', count: '50' } },
      { t: '05:30', action: 'email_phish', params: {} },
    ],
    objectives: [
      { id: 'obj-1', type: 'detection', validator: 'validate.opensearch_query', points: 60, params: {} },
      { id: 'obj-2', type: 'response', validator: 'validate.manual_ack', points: 40, params: { note: 'contain' } },
    ],
  };
}

describe('scenario-yaml.util', () => {
  describe('slug', () => {
    it('replaces whitespace, which the old builder never did', () => {
      // The previous regex escaped its own backslash and matched a literal "\s".
      expect(slug('Quick Detection Drill')).toBe('quick-detection-drill');
    });

    it('drops punctuation and collapses separators', () => {
      expect(slug('  APT29: Replay!!  ')).toBe('apt29-replay');
    });
  });

  describe('normalizeTime', () => {
    it('pads free-typed values into MM:SS', () => {
      expect(normalizeTime('5:3')).toBe('05:03');
      expect(normalizeTime('')).toBe('00:00');
      expect(normalizeTime('1230')).toBe('12:30');
    });

    it('clamps seconds so the document stays schema-valid', () => {
      expect(normalizeTime('0099')).toBe('00:59');
    });

    it('feeds a monotonic seconds value for the timeline track', () => {
      expect(timeToSeconds('05:30')).toBe(330);
      expect(timeToSeconds('00:00')).toBe(0);
    });
  });

  describe('toYaml', () => {
    it('emits every key the engine schema requires', () => {
      const yaml = toYaml(sample());
      for (const key of ['name:', 'version:', 'range_template:', 'timeline:', 'objectives:']) {
        expect(yaml).withContext(key).toContain(key);
      }
      expect(yaml).toContain('- t: "00:00"');
      expect(yaml).toContain('action: dns_spike');
      expect(yaml).toContain('points: 60');
    });

    it('quotes times and empty-ish values so YAML keeps them strings', () => {
      const yaml = toYaml(sample());
      expect(yaml).toContain('t: "05:30"');
      expect(yaml).not.toContain('t: 05:30');
    });

    it('writes empty collections as flow sequences, not dangling keys', () => {
      const yaml = toYaml(emptyScenario());
      expect(yaml).toContain('timeline: []');
      expect(yaml).toContain('objectives: []');
    });

    it('omits an empty params block', () => {
      const yaml = toYaml(sample());
      // The phish entry has no params; only two params blocks should exist.
      expect(yaml.match(/params:/g)?.length).toBe(2);
    });
  });

  describe('fromNormalized', () => {
    it('round-trips a document through the server-parsed shape', () => {
      const model = sample();
      // Mirrors what POST /scenarios/validate returns in `normalized`.
      const normalized = {
        name: model.name,
        version: model.version,
        description: model.description,
        range_template: model.range_template,
        timeline: [
          { t: '00:00', action: 'dns_spike', params: { domains: 'bad.example', count: 50 } },
          { t: '05:30', action: 'email_phish' },
        ],
        objectives: [
          { id: 'obj-1', type: 'detection', validator: 'validate.opensearch_query', points: 60 },
          { id: 'obj-2', type: 'response', validator: 'validate.manual_ack', points: 40, params: { note: 'contain' } },
        ],
      };
      const loaded = fromNormalized(normalized);
      expect(toYaml(loaded)).toBe(toYaml(model));
    });

    it('loads an invalid document rather than throwing, so it can be fixed', () => {
      const loaded = fromNormalized({ name: 'broken', timeline: 'not-a-list', objectives: null });
      expect(loaded.name).toBe('broken');
      expect(loaded.timeline).toEqual([]);
      expect(loaded.objectives).toEqual([]);
      expect(loaded.range_template).toBe('');
    });

    it('falls back to a known objective type when the document carries a bad one', () => {
      const loaded = fromNormalized({ objectives: [{ id: 'x', type: 'nonsense', validator: 'v', points: 1 }] });
      expect(loaded.objectives[0].type).toBe('detection');
    });

    it('flattens list params so they survive editing as text', () => {
      const loaded = fromNormalized({ timeline: [{ t: '00:10', action: 'a', params: { domains: ['x.com', 'y.com'] } }] });
      expect(loaded.timeline[0].params['domains']).toBe('x.com, y.com');
    });

    it('handles a null document', () => {
      expect(fromNormalized(null).timeline).toEqual([]);
    });
  });

  describe('totalPoints', () => {
    it('sums objective points so an author can reach 100', () => {
      expect(totalPoints(sample())).toBe(100);
      expect(totalPoints(emptyScenario())).toBe(0);
    });
  });
});
