/**
 * The shell's "Workspace / <title>" breadcrumb for a URL: the first path segment's section.
 * A page missing here showed "Overview" (Wiki, Support, Teams and Schedule did, #201).
 */
const TITLES: Record<string, string> = {
  dashboard: 'Overview',
  learning: 'Learning',
  authoring: 'Authoring',
  exercises: 'Exercises',
  'ops-center': 'Ops Center',
  'topology-3d': 'Exercises',
  schedule: 'Schedule',
  scoring: 'Review',
  telemetry: 'Telemetry',
  wiki: 'Wiki',
  support: 'Support',
  tickets: 'Support',
  teams: 'Teams',
  infrastructure: 'Infrastructure',
  'ai-orchestrator': 'AI Orchestrator',
  integrations: 'Integrations',
  users: 'People',
  approvals: 'People',
  admin: 'Administration',
};

export function workspaceTitleFor(url: string): string {
  const first = url.split('/')[1]?.split(/[?#]/)[0] ?? '';
  return TITLES[first] ?? 'Overview';
}
