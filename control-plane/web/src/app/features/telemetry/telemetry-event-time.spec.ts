import { telemetryEventTime } from './telemetry.component';

describe('telemetryEventTime', () => {
  it('prefers the pipeline timestamp', () => {
    expect(telemetryEventTime({ timestamp: '2026-10-04T12:00:00Z', '@timestamp': '1999-01-01T00:00:00Z' }))
      .toBe('2026-10-04T12:00:00Z');
  });

  it('falls back to @timestamp for events indexed by other shippers', () => {
    expect(telemetryEventTime({ '@timestamp': '2026-10-04T12:01:00Z' })).toBe('2026-10-04T12:01:00Z');
  });

  it('is empty when neither is present', () => {
    expect(telemetryEventTime({ event_type: 'dns_query' })).toBe('');
  });
});
