import { workspaceTitleFor } from './workspace-title';

describe('workspaceTitleFor (the shell breadcrumb, #201)', () => {
  it('names Wiki, Support, Teams and Schedule instead of falling back to Overview', () => {
    expect(workspaceTitleFor('/wiki')).toBe('Wiki');
    expect(workspaceTitleFor('/wiki/spaces/ops/pages/x')).toBe('Wiki');
    expect(workspaceTitleFor('/support')).toBe('Support');
    expect(workspaceTitleFor('/support/board?x=1')).toBe('Support');
    expect(workspaceTitleFor('/teams')).toBe('Teams');
    expect(workspaceTitleFor('/schedule')).toBe('Schedule');
  });

  it('keeps the existing sections', () => {
    expect(workspaceTitleFor('/exercises/abc')).toBe('Exercises');
    expect(workspaceTitleFor('/scoring?exercise=e1')).toBe('Review');
    expect(workspaceTitleFor('/learning/admin/progress')).toBe('Learning');
    expect(workspaceTitleFor('/dashboard')).toBe('Overview');
    expect(workspaceTitleFor('/')).toBe('Overview');
  });
});
