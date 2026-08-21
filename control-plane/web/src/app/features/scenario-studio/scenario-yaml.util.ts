/**
 * The scenario dialect the engine actually runs, as a typed model plus a
 * serializer and a loader.
 *
 * The old Scenario Builder emitted its own shape (id/difficulty/
 * duration_minutes/timeline[].delay_minutes) which fails the engine schema on
 * three required keys and every timeline entry, and it could only copy to the
 * clipboard. This module is the single place that knows the real shape:
 * `POST /scenarios/validate` parses YAML server-side and hands back
 * `normalized`, so nothing here has to parse YAML in the browser.
 */

export type ObjectiveType = 'detection' | 'response' | 'deliverable';

export interface TimelineEntry {
  /** Offset into the exercise as MM:SS — the schema's required format. */
  t: string;
  action: string;
  params: Record<string, string>;
}

export interface ScenarioObjective {
  id: string;
  type: ObjectiveType;
  validator: string;
  points: number;
  params: Record<string, string>;
}

export interface ScenarioModel {
  name: string;
  version: string;
  description: string;
  range_template: string;
  timeline: TimelineEntry[];
  objectives: ScenarioObjective[];
}

export const OBJECTIVE_TYPES: ObjectiveType[] = ['detection', 'response', 'deliverable'];

/** Validators the engine ships; free text is still allowed for custom ones. */
export const COMMON_VALIDATORS = [
  'validate.opensearch_query',
  'validate.manual_ack',
  'validate.deliverable_check',
];

export function emptyScenario(): ScenarioModel {
  return {
    name: '',
    version: '1.0',
    description: '',
    range_template: '',
    timeline: [],
    objectives: [],
  };
}

/** URL/id-safe slug. The old builder's regex escaped its own backslash and so
 *  matched a literal "\s", meaning spaces were never replaced. */
export function slug(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

/** Clamp a free-typed time to MM:SS, so the document stays schema-valid.
 *  A colon separates the fields when present ("5:3" is 05:03); without one the
 *  digits are read right-aligned ("1230" is 12:30, "45" is 00:45). */
export function normalizeTime(value: string): string {
  const raw = (value || '').trim();
  let mm: number;
  let ss: number;
  if (raw.includes(':')) {
    const [left, right] = raw.split(':');
    mm = parseInt(left.replace(/[^0-9]/g, ''), 10) || 0;
    ss = parseInt((right || '').replace(/[^0-9]/g, ''), 10) || 0;
  } else {
    const digits = raw.replace(/[^0-9]/g, '').slice(0, 4).padStart(4, '0');
    mm = parseInt(digits.slice(0, 2), 10) || 0;
    ss = parseInt(digits.slice(2), 10) || 0;
  }
  mm = Math.min(99, mm);
  ss = Math.min(59, ss);
  return `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
}

/** Minutes for a MM:SS stamp, for the proportional timeline track. */
export function timeToSeconds(t: string): number {
  const [mm, ss] = (t || '00:00').split(':');
  return (parseInt(mm, 10) || 0) * 60 + (parseInt(ss, 10) || 0);
}

function quote(value: string): string {
  // Quote anything YAML could read as a non-string (numbers, times, bools) or
  // that carries structural characters.
  const needs = value === '' || /^[\d.+-]|[:#{}[\],&*?|<>=!%@`"']|^\s|\s$/.test(value)
    || ['true', 'false', 'null', 'yes', 'no', 'on', 'off'].includes(value.toLowerCase());
  return needs ? `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"` : value;
}

function paramsBlock(params: Record<string, string>, indent: string): string[] {
  const keys = Object.keys(params || {}).filter(k => k && String(params[k]).length);
  if (!keys.length) return [];
  return [`${indent}params:`, ...keys.map(k => `${indent}  ${k}: ${quote(String(params[k]))}`)];
}

/** Serialize the model to engine-dialect YAML. */
export function toYaml(model: ScenarioModel): string {
  const lines: string[] = [
    `name: ${quote(model.name || 'untitled-scenario')}`,
    `version: ${quote(model.version || '1.0')}`,
  ];
  if (model.description) lines.push(`description: ${quote(model.description)}`);
  lines.push(`range_template: ${quote(model.range_template || 'small-enterprise')}`);

  lines.push('timeline:');
  if (!model.timeline.length) {
    lines[lines.length - 1] = 'timeline: []';
  } else {
    for (const entry of model.timeline) {
      lines.push(`  - t: ${quote(normalizeTime(entry.t))}`);
      if (entry.action) lines.push(`    action: ${quote(entry.action)}`);
      lines.push(...paramsBlock(entry.params, '    '));
    }
  }

  lines.push('objectives:');
  if (!model.objectives.length) {
    lines[lines.length - 1] = 'objectives: []';
  } else {
    for (const obj of model.objectives) {
      lines.push(`  - id: ${quote(obj.id)}`);
      lines.push(`    type: ${obj.type}`);
      lines.push(`    validator: ${quote(obj.validator)}`);
      lines.push(`    points: ${Number(obj.points) || 0}`);
      lines.push(...paramsBlock(obj.params, '    '));
    }
  }
  return lines.join('\n') + '\n';
}

function asParams(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    out[k] = Array.isArray(v) ? v.join(', ') : String(v ?? '');
  }
  return out;
}

/**
 * Build the model from the server's parsed document. Tolerant on purpose: an
 * invalid document must still load so the author can see and fix its errors.
 */
export function fromNormalized(doc: Record<string, any> | null | undefined): ScenarioModel {
  const model = emptyScenario();
  if (!doc) return model;
  model.name = String(doc['name'] ?? '');
  model.version = String(doc['version'] ?? '1.0');
  model.description = String(doc['description'] ?? '');
  model.range_template = String(doc['range_template'] ?? '');

  const timeline = Array.isArray(doc['timeline']) ? doc['timeline'] : [];
  model.timeline = timeline.filter(e => e && typeof e === 'object').map(e => ({
    t: normalizeTime(String(e['t'] ?? '')),
    action: String(e['action'] ?? ''),
    params: asParams(e['params']),
  }));

  const objectives = Array.isArray(doc['objectives']) ? doc['objectives'] : [];
  model.objectives = objectives.filter(o => o && typeof o === 'object').map(o => ({
    id: String(o['id'] ?? ''),
    type: (OBJECTIVE_TYPES.includes(o['type']) ? o['type'] : 'detection') as ObjectiveType,
    validator: String(o['validator'] ?? ''),
    points: Number(o['points']) || 0,
    params: asParams(o['params']),
  }));
  return model;
}

/** Points across all objectives — surfaced so an author can hit 100. */
export function totalPoints(model: ScenarioModel): number {
  return model.objectives.reduce((sum, o) => sum + (Number(o.points) || 0), 0);
}
