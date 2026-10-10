/** ARC² Course Studio: the API's shapes and the page's view rules (pure, so they are tested). */

export type Phase =
  | 'new' | 'queued' | 'running' | 'outline' | 'preview' | 'packaged'
  | 'paused' | 'stopped' | 'takeover' | 'failed';

export interface StageView { key: string; name: string; state: 'pending' | 'done' | 'failed' | 'running'; stop_reason?: string | null }
export interface Job {
  id: string; action: 'start' | 'resume'; state: 'queued' | 'running' | 'done' | 'failed';
  current_agent: string | null; error: string | null;
  created_at: string | null; started_at: string | null; finished_at: string | null;
}
export interface RunSummary {
  slug: string; name: string; title: string | null; code: string | null; request: string | null;
  phase: Phase; phase_text: string; stages: StageView[];
  gates: { outline: { state?: string; ts?: string; accepted_sha256?: string; accepted_by?: string }; preview: { state?: string; ts?: string; accepted_sha256?: string; rework_count?: number; accepted_by?: string } };
  qa: { result: string | null; cycle: number | null; rework_stage: string | null; checked_at: string | null };
  actions_open: number; actions_blocking: number; job: Job | null; updated_at: string | null;
  /** A test host's runner accepted a gate itself (ARC2_AUTO_ACCEPT_GATES): nobody reviewed the content. */
  auto_accepted?: boolean;
}
/** `auto`: the runner's own acceptance on a test host, not a person's. */
export interface Message { role: 'user' | 'pipeline'; text: string; ts: string | null; error?: boolean; auto?: boolean }
export interface QuizQuestion { question: string; options: string[]; answer: string }
export interface RunDetail extends RunSummary {
  messages: Message[];
  outline: { course?: string; duration_hours?: number; modules?: { id: string; title: string; minutes?: number; objective_ids?: string[] }[]; cuts?: string[] } | null;
  objectives: { id: string; text: string }[];
  modules: { title: string; quiz: QuizQuestion[] }[];
  pages: string[];
  lab: { range: string | null; injects: { id: string; t_offset_min: number; objective_id: string; critical: boolean; author_required: boolean; description: string }[]; noise_floor: { id: string; description: string }[] };
  findings: { check: string; severity: string; owner_stage: string; message: string }[];
  human_actions: HumanAction[];
  files: { path: string; stage: string; kind: string }[];
  package_ready: boolean;
}
export interface HumanAction {
  id: string; stage: string; category: string; text: string; blocks_promotion: boolean; status: string;
  ask?: 'decide' | 'supply' | 'confirm'; who?: string;
}

export type Ask = 'decide' | 'supply' | 'confirm';
export const ASK_LABEL: Record<Ask, string> = {
  decide: 'Decide: a judgement only an authority can make',
  supply: 'Supply: material only a person can provide',
  confirm: 'Confirm: approve something ARC² made',
};
const ASK_FROM_CATEGORY: Record<string, Ask> = { standards: 'decide', security: 'supply' };
const WHO_FROM_CATEGORY: Record<string, string> = {
  standards: 'Standards', security: 'Cleared author', infra: 'Range ops', content: 'Instructor', package: 'Instructor', qa: 'Instructor',
};

/** Open actions grouped as Decide / Supply / Confirm, blocking first. Older runs carry only a category. */
export function authorTodo(actions: HumanAction[]): { ask: Ask; items: (HumanAction & { ask: Ask; who: string })[] }[] {
  const open = actions.filter(a => a.status === 'open').map(a => ({
    ...a, ask: a.ask ?? ASK_FROM_CATEGORY[a.category] ?? 'confirm', who: a.who ?? WHO_FROM_CATEGORY[a.category] ?? 'Instructor',
  }));
  return (['decide', 'supply', 'confirm'] as Ask[])
    .map(ask => ({ ask, items: open.filter(a => a.ask === ask).sort((x, y) => Number(y.blocks_promotion) - Number(x.blocks_promotion)) }))
    .filter(g => g.items.length);
}

export interface RunList { runs: RunSummary[]; runner_seen: string | null }

export interface PrimaryAction { label: string; enabled: boolean; hint: string; kind?: 'accept' | 'retry' }

/** The big button under the chat: what it says, and whether it does anything now. */
export function primaryAction(run: RunSummary | null): PrimaryAction {
  if (!run) return { label: 'Waiting to start', enabled: false, hint: 'Describe the course, then Send' };
  // The last step failed before ARC² could do anything with it: offer to repeat it.
  if (run.job?.state === 'failed') return { label: '↻ Try again', enabled: true, hint: run.job.error ?? 'The last step failed', kind: 'retry' };
  switch (run.phase) {
    case 'outline': return { label: 'Accept Outline', enabled: true, hint: 'or type feedback below to refine the outline', kind: 'accept' };
    case 'preview': return { label: '✓ Approve & Package', enabled: true, hint: 'or type feedback below to request changes', kind: 'accept' };
    case 'packaged': return { label: '✓ Packaged', enabled: false, hint: 'Package candidate · not published' };
    case 'queued': case 'running': return { label: 'Working…', enabled: false, hint: run.phase_text };
    // Stopped between stages (for example a safety check): /arc2 --resume … accept carries on from the manifest.
    case 'paused': case 'stopped': return { label: '▶ Continue', enabled: true, hint: run.phase_text, kind: 'accept' };
    case 'takeover': case 'failed': return { label: 'Needs a person', enabled: false, hint: run.phase_text };
    default: return { label: 'Waiting', enabled: false, hint: run.phase_text };
  }
}

/** Feedback goes to /arc2 only at a review. */
export function canReply(run: RunSummary | null): boolean {
  return !!run && (run.phase === 'outline' || run.phase === 'preview');
}

/** Refresh often while ARC² is working, rarely otherwise. */
export function pollMs(run: RunSummary | null): number {
  return run && (run.phase === 'queued' || run.phase === 'running') ? 4000 : 30000;
}

/** Stage dots for the agent bar: the one the runner reports as working is 'running'. */
export function stageDots(run: RunSummary | null): StageView[] {
  if (!run) return [];
  const working = run.job?.state === 'running' ? run.job.current_agent : null;
  return run.stages.map(s => (s.name === working ? { ...s, state: 'running' } : s));
}

/** True when a job has waited a minute and the runner has not touched anything since it was queued. */
export function runnerLooksIdle(run: RunSummary | null, runnerSeen: string | null, now = Date.now()): boolean {
  const job = run?.job;
  if (!job || job.state !== 'queued' || !job.created_at) return false;
  const queued = Date.parse(job.created_at);
  if (now - queued < 60_000) return false;
  return !runnerSeen || Date.parse(runnerSeen) < queued;
}

/** "02-content/mod_001/content/page-02.html" → { module: 'mod_001', page: 2 }. */
export function pageRef(path: string): { module: string; page: number } | null {
  const m = /(mod_\d+)\/content\/page-(\d+)\.html$/.exec(path);
  return m ? { module: m[1], page: Number(m[2]) } : null;
}

export function shortTime(ts: string | null | undefined): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
