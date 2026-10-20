/**
 * How an exercise objective reads to a person (issue #201): its name rather than its ref
 * id, who judges it, and its evidence as a sentence rather than the stored JSON.
 * Display only: achievement and credit come from the API, never from here.
 */

/** Enough of an objective (ObjectiveOut or the scenario-detail row) to name it. */
export interface NamedObjective {
  ref_id: string;
  description?: string | null;
}

/** Enough of an objective to say who judges it. */
export interface JudgedObjective {
  validator?: string | null;
  type?: string | null;
  objective_type?: string | null;
}

const UPPER_TOKEN = /^(?=.*\d)[a-z]{1,4}\d+$|^[a-z]\d+$/i;

/** `contain_ws07` -> "Contain WS07", `crit-2` -> "Critical event 2" (qsp_paths ids). */
export function humanizeRef(ref: string): string {
  const crit = /^crit-(\d+)$/i.exec(ref.trim());
  if (crit) return `Critical event ${crit[1]}`;
  const words = ref.trim().split(/[_\-\s.]+/).filter(Boolean)
    .map(w => (UPPER_TOKEN.test(w) ? w.toUpperCase() : w.toLowerCase()));
  if (!words.length) return ref;
  words[0] = words[0].charAt(0).toUpperCase() + words[0].slice(1);
  return words.join(' ');
}

/** The objective's name; its id turned into words when the scenario gave it none. */
export function objectiveName(o: NamedObjective): string {
  const name = (o.description ?? '').trim();
  return name && name !== o.ref_id ? name : humanizeRef(o.ref_id);
}

/** Canonical validator name: content may write `validate.opensearch_query`. */
function bareValidator(v: string | null | undefined): string {
  const s = (v ?? '').trim();
  return s.includes('.') ? s.slice(s.lastIndexOf('.') + 1) : s;
}

/**
 * Who judges it (scenario-engine objective_catalogue.py). An objective with no validator
 * is the instructor's to decide (DEFAULT_VALIDATOR is manual_ack), and a deliverable is
 * signed off by an instructor until the platform takes submissions.
 */
export function objectiveKind(o: JudgedObjective): string {
  switch (bareValidator(o.validator)) {
    case '':
    case 'manual_ack':
      return 'Instructor sign-off';
    case 'deliverable_check':
      return 'Deliverable · instructor sign-off';
    case 'opensearch_query':
      return 'Detection';
    case 'greyspace_breadcrumb':
      return 'Greyspace breadcrumb';
    case 'user_disabled':
      return 'Response · observed telemetry';
  }
  const t = (o.type ?? o.objective_type ?? '').trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Objective';
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function sentenceCase(key: string): string {
  const s = key.replace(/_/g, ' ').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Evidence as a line a person reads. A credited detection (app/routers/detections.py
 * `_credit`) says who detected it and what their query found; other JSON becomes
 * "Label: value" pairs of its plain fields; text is shown as it is.
 */
export function evidenceText(raw: string | null | undefined): string {
  const text = (raw ?? '').trim();
  if (!text) return '';
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return text;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return text;
  const d = data as Record<string, unknown>;
  if (d['source'] === 'student_detection' || d['source'] === 'staff_detection') {
    const staff = d['source'] === 'staff_detection';
    const who = String((staff ? d['user'] : d['student']) ?? '').trim() || (staff ? 'staff' : 'a Student');
    const found: string[] = [];
    if (typeof d['events_matched'] === 'number') found.push(plural(d['events_matched'], 'event matched', 'events matched'));
    if (typeof d['on_target'] === 'number') found.push(`${d['on_target']} of the attack`);
    return `Detected by ${who}${staff ? ' (staff check)' : ''}${found.length ? ': ' + found.join(', ') : ''}`;
  }
  const parts = Object.entries(d)
    .filter(([k, v]) => !k.endsWith('_id') && v !== null && ['string', 'number', 'boolean'].includes(typeof v))
    .map(([k, v]) => `${sentenceCase(k)}: ${typeof v === 'string' ? v.replace(/_/g, ' ') : String(v)}`);
  return parts.join(' · ');
}
