import { Injectable, inject } from '@angular/core';
import { Observable, shareReplay } from 'rxjs';
import { ApiService } from './api.service';

/** A developmental period — one column of the career map. */
export interface DPStage {
  dp_order: number;
  rank_level: string;
  label: string;
  /** No qualification ingested for this period yet; drawn as a ghost placeholder. */
  planned: boolean;
}

/** A swimlane — the rank ladder, or one specialty stream. */
export interface DPTrack {
  key: string;
  label: string;
  kind: 'progression' | 'specialty';
}

export type POProgressState = 'not_started' | 'in_progress' | 'completed' | 'failed';
export type NodeState = 'locked' | 'available' | 'in_progress' | 'complete';

export interface Lesson {
  id: string;
  title: string;
  duration_minutes: number;
  is_published: boolean;
}

export interface EO {
  id: string;
  eo_code: string;
  title: string;
  maps_to_critical_event: string | null;
  lessons: Lesson[];
}

export interface CompTag {
  framework: string;
  code: string;
  name: string;
  relation: string;
}

/** The course and module that actually deliver an objective. */
export interface DeliveredBy {
  course_id: string;
  course_code: string;
  course_name: string;
  module_id: string;
  module_title: string;
  /** The developmental period the course is *taught* in. */
  dp_order: number;
  institution: string;
  term_code: string;
  /** The deliverer is a spine-generated stub, i.e. the objective is not really covered. */
  is_placeholder: boolean;
}

/** An objective a course delivers, for the course-side view of the same link. */
export interface CourseDelivers {
  qsp_code: string;
  dp_order: number;
  po_code: string;
  po_title: string;
  module_id: string;
  module_title: string;
}

/** A catalogue course taught in this node's developmental period. */
export interface NodeCourse {
  course_id: string;
  course_code: string;
  name: string;
  institution: string;
  term_code: string;
  term_label: string;
  term_start: string;
  duration_hours: number;
  difficulty: string;
  is_published: boolean;
  delivers: CourseDelivers[];
  /** Taught in this period but delivering into another one. */
  delivers_cross_dp: boolean;
}

export interface PO {
  id: string;
  po_code: string;
  title: string;
  tier: string;
  environment: string;
  status: string;
  duration_min: number;
  critical_events: string[];
  target_role: string;
  nice_dcwf_task: string;
  scenario_count: number;
  /** What the candidate is given going in. */
  conditions: string;
  /** How they are tested (e.g. "PC written+practical"). */
  assessment_type: string;
  /** What counts as a pass. */
  pass_standard: string;
  /** What they hand in. */
  deliverable: string;
  /** Authoring effort to build this assessment — not learner time. */
  build_hours: number;
  enabling_objectives: EO[];
  competencies: CompTag[];
  exercise_id: string | null;
  scenario_id: string | null;
  range_id: string | null;
  course_code: string | null;
  /** Null when nothing delivers this objective yet. */
  delivered_by: DeliveredBy | null;
  duration_long: boolean;
  progress_state: POProgressState;
}

export interface NodeProgress {
  completed: number;
  in_progress: number;
  total: number;
  pct: number;
}

/** One qualification, placed at (track_key, dp_order) on the map. */
export interface QualNode {
  qsp_code: string;
  nqual: string;
  title: string;
  dp_order: number;
  track: string;
  track_key: string;
  rank_level: string;
  po_count: number;
  gate_count: number;
  total_minutes: number;
  /** Objectives carrying a duration. Below `po_count`, the total is a floor. */
  timed_po_count: number;
  /** Taught hours across this period's programme — distinct from assessment time. */
  course_hours: number;
  state: NodeState;
  progress: NodeProgress;
  objectives: PO[];
  /** The programme taught in this period, in term order. */
  courses: NodeCourse[];
  course_count: number;
}

/** A derived prerequisite link. `to` is `planned:<dp>` for the ghost tail. */
export interface PathEdge {
  from: string;
  to: string;
  kind: 'progression' | 'branch' | 'planned';
}

/** A course-to-course prerequisite: `to` is taken after `from`. */
export interface CourseEdge {
  from: string;
  to: string;
}

export interface CurriculumMap {
  stages: DPStage[];
  tracks: DPTrack[];
  nodes: QualNode[];
  edges: PathEdge[];
  /** Only edges whose both ends appear in `nodes[].courses` — always drawable. */
  course_edges: CourseEdge[];
  learner: { current_qsp_code: string | null; current_po_code: string | null };
}

/**
 * The career map, fetched once and replayed.
 *
 * The whole map arrives in a single response, so the qualifications page no longer
 * fans out one request per qualification. The replay means moving between Learning
 * hub tabs and back does not refetch; call `refresh()` after anything that changes
 * the spine (crosswalk import, path generation) or the learner's progress.
 */
@Injectable({ providedIn: 'root' })
export class CurriculumMapService {
  private readonly api = inject(ApiService);
  private cached?: Observable<CurriculumMap>;

  /** The map, from cache when already loaded. */
  map(): Observable<CurriculumMap> {
    this.cached ??= this.api
      .get<CurriculumMap>('/qsp/curriculum-map')
      // refCount stays false so the response survives the last subscriber
      // unsubscribing — that is the whole point of caching across tab switches.
      .pipe(shareReplay({ bufferSize: 1, refCount: false }));
    return this.cached;
  }

  /** Drop the cache and fetch again. */
  refresh(): Observable<CurriculumMap> {
    this.cached = undefined;
    return this.map();
  }
}

/** Courses grouped under the term that teaches them. */
export interface TermGroup {
  code: string;
  label: string;
  /** "Y1 Fall" — the long label with the DP prefix stripped, for tight layouts. */
  short: string;
  courses: NodeCourse[];
}

/**
 * Group a node's programme by term, in delivery order.
 *
 * The backend already sorts courses by `(term_start, term_code, course_code)`, so this
 * only has to preserve first-seen order. Shared by the career map and the detail panel
 * so the two can never disagree about how a period is divided.
 */
export function groupByTerm(courses: NodeCourse[]): TermGroup[] {
  const groups = new Map<string, TermGroup>();
  for (const c of courses) {
    const code = c.term_code || 'unscheduled';
    let group = groups.get(code);
    if (!group) {
      group = { code, label: c.term_label, short: shortTermLabel(c), courses: [] };
      groups.set(code, group);
    }
    group.courses.push(c);
  }
  return [...groups.values()];
}

/**
 * A compact term name for the map, since the DP column already names the period:
 *   "DP 1 Year 1 Fall — Foundations I"   -> "Y1 Fall"
 *   "DP 2 Fall — Advanced Technical I"   -> "Fall"
 *
 * Falls back to the term code, then the full label — a term that does not follow the
 * catalogue's naming still has to render as something rather than blank.
 */
function shortTermLabel(c: NodeCourse): string {
  const label = c.term_label || '';
  const year = /Year\s+(\d+)\s+(\w+)/i.exec(label);
  if (year) return `Y${year[1]} ${year[2]}`;
  // A single-year period names only its season.
  const season = /^DP\s*\d+\s+(\w+)/i.exec(label);
  if (season) return season[1];
  const code = /^DP\d+-(.+)$/i.exec(c.term_code || '');
  if (code) return code[1].replace(/-/g, ' ');
  return label || c.term_code || 'Unscheduled';
}
