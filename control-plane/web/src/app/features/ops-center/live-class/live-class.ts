/**
 * How the live class reads (issue #199, layout B). Display only: every fact here comes from
 * GET /ops/exercises/{id}/class; nothing is scored, flagged or changed on the client.
 *
 * The four attention lanes follow R1 F2 and are never merged. Help requests are the F2 model
 * and do not exist yet, so that lane is always empty and says so. Idle is never a learning
 * signal, and the word for it is neutral.
 */
import type { ClassAttention, ClassStudent } from '@core/services/api.service';

export type LaneKey = 'help' | 'lab' | 'learning' | 'quiet';
export type StatusKey = 'lab_fault' | 'lab_wait' | 'learning' | 'not_seen' | 'ok';

export interface LaneMeta {
  key: LaneKey;
  title: string;
  hint: string;
}

export const LANES: readonly LaneMeta[] = [
  { key: 'help', title: 'Asked for help', hint: 'Help requests arrive with the intervention console (R1 F2).' },
  { key: 'lab', title: 'Lab or infrastructure', hint: "Not the Student's doing. Not counted against them." },
  { key: 'learning', title: 'Learning signals', hint: 'From judged detections only: 3 or more misses on one objective.' },
  { key: 'quiet', title: 'Not seen recently', hint: 'No detection or lab heartbeat lately. Idle is not a learning signal.' },
];

const LANE_OF: Record<ClassAttention['kind'], LaneKey> = {
  lab_fault: 'lab', lab_wait: 'lab', learning: 'learning', not_seen: 'quiet',
};

/** Priority of a Student's headline status (help would come first once it exists). */
export const STATUS_RANK: Record<StatusKey, number> = { lab_fault: 1, lab_wait: 2, learning: 3, not_seen: 4, ok: 5 };

export const STATUS_WORD: Record<StatusKey, string> = {
  lab_fault: 'Lab fault', lab_wait: 'Lab not ready', learning: 'Learning signal', not_seen: 'Not seen recently',
  ok: 'No flags',
};

export interface LaneItem {
  student: ClassStudent;
  item: ClassAttention;
}

/** Every attention item of every Student, into its lane. A Student may sit in several. */
export function lanesOf(students: readonly ClassStudent[]): Record<LaneKey, LaneItem[]> {
  const out: Record<LaneKey, LaneItem[]> = { help: [], lab: [], learning: [], quiet: [] };
  for (const student of students) {
    for (const item of student.attention ?? []) out[LANE_OF[item.kind]].push({ student, item });
  }
  // Oldest first: whoever has waited longest is at the top.
  for (const k of Object.keys(out) as LaneKey[]) {
    out[k].sort((a, b) => (a.item.since ?? '').localeCompare(b.item.since ?? ''));
  }
  return out;
}

/** The Student's flags in priority order; "ok" when there are none. */
export function statusesOf(s: ClassStudent): StatusKey[] {
  const keys = (s.attention ?? []).map(a => a.kind as StatusKey);
  const uniq = [...new Set(keys)].sort((a, b) => STATUS_RANK[a] - STATUS_RANK[b]);
  return uniq.length ? uniq : ['ok'];
}

export function headline(s: ClassStudent): StatusKey {
  return statusesOf(s)[0];
}

export type SortKey = 'status' | 'name' | 'team' | 'detections' | 'activity';

/** Ascending comparators; the table flips them for descending. */
export const SORTERS: Record<SortKey, (a: ClassStudent, b: ClassStudent) => number> = {
  status: (a, b) => STATUS_RANK[headline(a)] - STATUS_RANK[headline(b)] || byName(a, b),
  name: (a, b) => byName(a, b),
  team: (a, b) => (a.team?.name ?? '￿').localeCompare(b.team?.name ?? '￿') || byName(a, b),
  detections: (a, b) => (a.attempts_credited ?? 0) - (b.attempts_credited ?? 0)
    || (a.attempts_total ?? 0) - (b.attempts_total ?? 0) || byName(a, b),
  // Least recently active first: no activity at all sorts before any.
  activity: (a, b) => (a.last_activity_at ?? '').localeCompare(b.last_activity_at ?? '') || byName(a, b),
};

function byName(a: ClassStudent, b: ClassStudent): number {
  return a.display_name.localeCompare(b.display_name);
}

export function sortStudents(list: readonly ClassStudent[], key: SortKey, dir: 'asc' | 'desc'): ClassStudent[] {
  const cmp = SORTERS[key];
  return [...list].sort((a, b) => (dir === 'asc' ? cmp(a, b) : cmp(b, a)));
}

/** "3 min ago", "1 h 5 min ago"; "never" when there is nothing to measure. */
export function ago(iso: string | null | undefined, now: Date): string {
  if (!iso) return 'never';
  const mins = Math.max(0, Math.floor((now.getTime() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  return `${h} h ${mins % 60} min ago`;
}

/** "T+1:12" relative to the exercise start. */
export function tplus(iso: string | null | undefined, started: string | null | undefined): string {
  if (!iso || !started) return '';
  const mins = Math.max(0, Math.floor((new Date(iso).getTime() - new Date(started).getTime()) / 60000));
  return `T+${Math.floor(mins / 60)}:${String(mins % 60).padStart(2, '0')}`;
}

export const VERDICT_WORD: Record<string, string> = {
  achieved: 'Credited', missed: 'Missed', pending: 'Verdict pending', closed: 'Closed (exercise ended)',
};

export const OBJECTIVE_STATE_WORD: Record<string, string> = {
  credited: 'Credited', pending: 'Verdict pending', missed: 'Missed', not_attempted: 'Not attempted',
};

/** Labs that POST /lab-sessions/{id}/reset accepts (lab_sessions/service.py `reset`). */
export const RESETTABLE_LAB = ['ready', 'active'];
