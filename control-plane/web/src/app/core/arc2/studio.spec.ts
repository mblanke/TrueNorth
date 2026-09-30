import { RunSummary, canReply, pageRef, pollMs, primaryAction, runnerLooksIdle, stageDots } from './studio';

const run = (over: Partial<RunSummary> = {}): RunSummary => ({
  slug: 'arc2-wireshark', name: 'Wireshark', title: null, code: null, request: 'x',
  phase: 'outline', phase_text: 'Outline ready for your review',
  stages: [
    { key: 'content-architect', name: 'Content Architect', state: 'done' },
    { key: 'code-generator', name: 'Code Generator', state: 'pending' },
  ],
  gates: { outline: { state: 'pending' }, preview: { state: 'n/a' } },
  qa: { result: null, cycle: null, rework_stage: null, checked_at: null },
  actions_open: 0, actions_blocking: 0, job: null, updated_at: null, ...over,
});

describe('ARC² studio view rules', () => {
  it('offers the review that is pending, and nothing while ARC² works', () => {
    expect(primaryAction(run()).label).toBe('Accept Outline');
    expect(primaryAction(run({ phase: 'preview' })).label).toBe('✓ Approve & Package');
    expect(primaryAction(run({ phase: 'running', phase_text: 'Code Generator is working' }))).toEqual(
      { label: 'Working…', enabled: false, hint: 'Code Generator is working' });
    expect(primaryAction(run({ phase: 'packaged' })).enabled).toBeFalse();
    expect(primaryAction(null).enabled).toBeFalse();
  });

  it('offers to repeat a step that failed', () => {
    const failed = run({ phase: 'failed', job: { id: 'j', action: 'start', state: 'failed', current_agent: null,
      error: 'Failed to authenticate', created_at: null, started_at: null, finished_at: null } });
    expect(primaryAction(failed)).toEqual({ label: '↻ Try again', enabled: true, hint: 'Failed to authenticate', kind: 'retry' });
  });

  it('takes feedback only at a review', () => {
    expect(canReply(run())).toBeTrue();
    expect(canReply(run({ phase: 'queued' }))).toBeFalse();
    expect(canReply(run({ phase: 'packaged' }))).toBeFalse();
  });

  it('polls fast only while working', () => {
    expect(pollMs(run({ phase: 'running' }))).toBe(4000);
    expect(pollMs(run())).toBe(30000);
  });

  it('marks the agent the runner reports as working', () => {
    const r = run({ phase: 'running', job: { id: 'j', action: 'resume', state: 'running', current_agent: 'Code Generator',
      error: null, created_at: null, started_at: null, finished_at: null } });
    expect(stageDots(r).map(s => s.state)).toEqual(['done', 'running']);
  });

  it('says when a queued job has not been picked up for a minute', () => {
    const queuedAt = '2026-09-30T12:00:00.000Z';
    const r = run({ phase: 'queued', job: { id: 'j', action: 'start', state: 'queued', current_agent: null, error: null,
      created_at: queuedAt, started_at: null, finished_at: null } });
    const t = Date.parse(queuedAt);
    expect(runnerLooksIdle(r, null, t + 30_000)).toBeFalse();
    expect(runnerLooksIdle(r, null, t + 90_000)).toBeTrue();
    expect(runnerLooksIdle(r, '2026-09-30T12:00:30.000Z', t + 90_000)).toBeFalse();
  });

  it('reads module and page from a content path', () => {
    expect(pageRef('02-content/mod_002/content/page-03.html')).toEqual({ module: 'mod_002', page: 3 });
    expect(pageRef('02-content/arc2.yaml')).toBeNull();
  });
});
