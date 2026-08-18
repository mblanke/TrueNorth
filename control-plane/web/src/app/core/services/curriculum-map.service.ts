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
  state: NodeState;
  progress: NodeProgress;
  objectives: PO[];
}

/** A derived prerequisite link. `to` is `planned:<dp>` for the ghost tail. */
export interface PathEdge {
  from: string;
  to: string;
  kind: 'progression' | 'branch' | 'planned';
}

export interface CurriculumMap {
  stages: DPStage[];
  tracks: DPTrack[];
  nodes: QualNode[];
  edges: PathEdge[];
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
