import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import {
  AAR, Exercise, ExerciseSummary, HealthResponse, HypervisorNode, InjectRecord, Objective, Range,
  RangeDocument, RangeSummary, Scenario, ScenarioSummary, Team, Template, TemplateSummary,
  Tenant, TelemetryEvent, User,
} from '../models';
import type { components } from '../api/schema';

/** Result of POST /scenarios/validate and /templates/validate. */
export interface YamlValidation {
  valid: boolean;
  errors: { path: string; message: string }[];
  normalized: Record<string, any> | null;
}

/** One entry of GET /injectors. */
export interface InjectorInfo {
  name: string;
  description: string;
  required_params: string[];
  mitre_techniques: string[];
}

/** GET /ranges/stats. `by_state` may be absent. */
export type RangeStats = components['schemas']['RangeStatsOut'];
/** One detection attempt (POST/GET .../detections). */
export type Detection = components['schemas']['DetectionOut'];

/** One row of GET /exercise-forge/history. */
export interface ForgeHistoryItem {
  id: string;
  created_at: string | null;
  exercise_id: string;
  scenario_id: string;
  exercise_name: string;
  source: string;
  difficulty: string;
  model_used: string;
  mitre_techniques: string[];
}

/** A collective exercise summary (list) and its detail (get). */
export interface CollectiveExercise {
  id: string;
  name: string;
  state: string;
  range_id: string;
  objectives: number;
  mesl_events: number;
}
export interface MeslEvent {
  id: string;
  serial: number;
  phase: string;
  scenario_time: string;
  title: string;
  description: string;
  objective_ref: string;
  attack_technique: string;
  delivery_method: string;
  from_cell: string;
  to_participant: string;
  expected_action: string;
  moe: string;
  status: string;
}
export interface CollectiveExerciseDetail {
  id: string;
  name: string;
  state: string;
  range_id: string;
  objectives: { id: string; ref: string; text: string; moe: string; competency_code: string }[];
  mesl: MeslEvent[];
}


/** Result of POST /ranges/{id}/topology. */
export interface RangeTopologySave {
  range_id: string;
  template_id: string;
  created: boolean;
  node_count: number;
  vlan_count: number;
  warnings: string[];
  template: Record<string, unknown>;
}

/** GET /competency/heatmap. */
export interface CompetencyHeatmap {
  categories: string[];
  work_roles: string[];
  values: number[][]; // [categoryIdx, roleIdx, score 0-100]
}

/** Result of POST /templates/from-diagram: the same template as a dict and as YAML. */
export interface DiagramTemplate {
  template: Record<string, unknown>;
  yaml: string;
  warnings: string[];
  /** Windows Server role placements that cannot be built; Save topology refuses them. */
  errors?: string[];
}

/** Minimum / recommended sizing of a Windows Server role. */
export type RoleSpecs = components['schemas']['RoleSpecsOut'];
/** One Windows Server role (GET /templates/windows-roles). `method`: feature (installed
 *  after boot) or image (cloned from a pre-built role image, one per VM). */
export type WindowsRole = components['schemas']['WindowsRoleOut'];
export type WindowsRoleCatalogue = components['schemas']['WindowsRoleCatalogueOut'];

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  private base = environment.apiUrl;

  // ── Generic HTTP helpers (used by LMS/directory components) ──
  get<T>(path: string): Observable<T> {
    return this.http.get<T>(`${this.base}${path}`);
  }
  post<T>(path: string, body: any): Observable<T> {
    return this.http.post<T>(`${this.base}${path}`, body);
  }
  put<T>(path: string, body: any): Observable<T> {
    return this.http.put<T>(`${this.base}${path}`, body);
  }
  patch<T>(path: string, body: any): Observable<T> {
    return this.http.patch<T>(`${this.base}${path}`, body);
  }
  delete<T>(path: string): Observable<T> {
    return this.http.delete<T>(`${this.base}${path}`);
  }

  // ── Health ───────────────────────────────────────────────
  health(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.base}/health`);
  }

  // ── Tenants ──────────────────────────────────────────────
  listTenants(): Observable<Tenant[]> {
    return this.http.get<Tenant[]>(`${this.base}/tenants`);
  }
  createTenant(data: { name: string; slug: string }): Observable<Tenant> {
    return this.http.post<Tenant>(`${this.base}/tenants`, data);
  }
  updateTenant(id: string, data: Partial<Tenant>): Observable<Tenant> {
    return this.http.put<Tenant>(`${this.base}/tenants/${id}`, data);
  }

  // ── Templates ────────────────────────────────────────────
  listTemplates(limit = 50, offset = 0): Observable<TemplateSummary[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<TemplateSummary[]>(`${this.base}/templates`, { params });
  }
  getTemplate(id: string): Observable<Template> {
    return this.http.get<Template>(`${this.base}/templates/${id}`);
  }
  createTemplate(data: Partial<Template>): Observable<Template> {
    return this.http.post<Template>(`${this.base}/templates`, data);
  }
  updateTemplate(id: string, data: Partial<Template>): Observable<Template> {
    return this.http.put<Template>(`${this.base}/templates/${id}`, data);
  }
  deleteTemplate(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/templates/${id}`);
  }

  // ── Scenarios ────────────────────────────────────────────
  listScenarios(limit = 50, offset = 0): Observable<ScenarioSummary[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<ScenarioSummary[]>(`${this.base}/scenarios`, { params });
  }
  getScenario(id: string): Observable<Scenario> {
    return this.http.get<Scenario>(`${this.base}/scenarios/${id}`);
  }
  createScenario(data: Partial<Scenario>): Observable<Scenario> {
    return this.http.post<Scenario>(`${this.base}/scenarios`, data);
  }
  updateScenario(id: string, data: Partial<Scenario>): Observable<Scenario> {
    return this.http.put<Scenario>(`${this.base}/scenarios/${id}`, data);
  }
  deleteScenario(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/scenarios/${id}`);
  }

  // ── Ranges ───────────────────────────────────────────────
  listRanges(limit = 50, offset = 0): Observable<RangeSummary[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<RangeSummary[]>(`${this.base}/ranges`, { params });
  }
  getRange(id: string): Observable<Range> {
    return this.http.get<Range>(`${this.base}/ranges/${id}`);
  }
  createRange(data: { name: string; template_id: string }): Observable<Range> {
    return this.http.post<Range>(`${this.base}/ranges`, data);
  }
  provisionRange(id: string): Observable<Range> {
    return this.http.post<Range>(`${this.base}/ranges/${id}/provision`, {});
  }
  stopRange(id: string): Observable<Range> {
    return this.http.post<Range>(`${this.base}/ranges/${id}/stop`, {});
  }
  startRange(id: string): Observable<Range> {
    return this.http.post<Range>(`${this.base}/ranges/${id}/start`, {});
  }
  updateRange(id: string, data: Partial<Range>): Observable<Range> {
    return this.http.put<Range>(`${this.base}/ranges/${id}`, data);
  }
  destroyRange(id: string): Observable<Range> {
    return this.http.post<Range>(`${this.base}/ranges/${id}/destroy`, {});
  }
  getRangeDiagram(id: string): Observable<{ range_id: string; diagram_json: any }> {
    return this.http.get<{ range_id: string; diagram_json: any }>(`${this.base}/ranges/${id}/diagram`);
  }
  saveRangeDiagram(id: string, diagram: any): Observable<{ range_id: string; diagram_json: any }> {
    return this.http.put<{ range_id: string; diagram_json: any }>(`${this.base}/ranges/${id}/diagram`, diagram);
  }
  /**
   * Make a designer diagram the topology the range provisions: the server converts
   * it to a template (range-owned) and repoints the range. 409 once the range has VMs.
   */
  saveRangeTopology(id: string, diagram: any): Observable<RangeTopologySave> {
    return this.http.post<RangeTopologySave>(`${this.base}/ranges/${id}/topology`, { diagram_json: diagram });
  }
  getRangeStats(): Observable<RangeStats> {
    return this.http.get<RangeStats>(`${this.base}/ranges/stats`);
  }
  /** Replace a range's description with the contents of a text/markdown file. */
  importRangeDescription(id: string, file: File): Observable<Range> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<Range>(`${this.base}/ranges/${id}/description/import`, form);
  }
  listRangeDocuments(id: string): Observable<RangeDocument[]> {
    return this.http.get<RangeDocument[]>(`${this.base}/ranges/${id}/documents`);
  }
  uploadRangeDocuments(id: string, files: File[]): Observable<RangeDocument[]> {
    const form = new FormData();
    for (const f of files) form.append('files', f);
    return this.http.post<RangeDocument[]>(`${this.base}/ranges/${id}/documents`, form);
  }
  deleteRangeDocument(id: string, documentId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/ranges/${id}/documents/${documentId}`);
  }
  /** Direct link for downloading an attachment in its original form. */
  rangeDocumentUrl(id: string, documentId: string): string {
    return `${this.base}/ranges/${id}/documents/${documentId}`;
  }

  // ── Authoring: validation, catalogues, AI drafts ─────────
  /** Validate scenario YAML against the engine schema (see POST /scenarios/validate). */
  validateScenario(yaml: string): Observable<YamlValidation> {
    return this.http.post<YamlValidation>(`${this.base}/scenarios/validate`, { yaml });
  }
  validateTemplate(yaml: string): Observable<YamlValidation> {
    return this.http.post<YamlValidation>(`${this.base}/templates/validate`, { yaml });
  }
  /** Starter topology rendered from a template's declared assets. */
  templateDiagramPreview(id: string): Observable<{ template_id: string; diagram_json: any }> {
    return this.http.post<{ template_id: string; diagram_json: any }>(
      `${this.base}/templates/${id}/diagram-preview`, {},
    );
  }
  /** Convert a designer diagram to the template Save topology would provision (no write). */
  templateFromDiagram(diagram: any, name = 'Range Design'): Observable<DiagramTemplate> {
    return this.http.post<DiagramTemplate>(`${this.base}/templates/from-diagram`, { diagram_json: diagram, name });
  }
  /** The real injector registry — replaces hard-coded action lists. */
  listInjectors(): Observable<InjectorInfo[]> {
    return this.http.get<InjectorInfo[]>(`${this.base}/injectors`);
  }
  /** Windows Server roles with minimum sizing and placement rules, for the designer. */
  getWindowsRoles(): Observable<WindowsRoleCatalogue> {
    return this.http.get<WindowsRoleCatalogue>(`${this.base}/templates/windows-roles`);
  }
  /** Hypervisor-verified OS aliases for the designer's image picker. */
  getGoldenImageAliasMap(hypervisor?: string): Observable<Record<string, string>> {
    let params = new HttpParams();
    if (hypervisor) params = params.set('hypervisor', hypervisor);
    return this.http.get<Record<string, string>>(`${this.base}/golden-images/alias-map`, { params });
  }
  aiScenarioDraft(body: { objectives: string[]; difficulty?: string; duration_minutes?: number }):
    Observable<{ output: string; model_used: string }> {
    return this.http.post<{ output: string; model_used: string }>(`${this.base}/ai/scenario-draft`, body);
  }
  aiDetectionDraft(body: { technique: string; data_source?: string; format?: string }):
    Observable<{ output: string; model_used: string }> {
    return this.http.post<{ output: string; model_used: string }>(`${this.base}/ai/detection-draft`, body);
  }
  getForgeHistory(limit = 50, offset = 0): Observable<{ items: ForgeHistoryItem[]; total: number }> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<{ items: ForgeHistoryItem[]; total: number }>(
      `${this.base}/exercise-forge/history`, { params },
    );
  }

  // ── Collective exercises / MESL ──────────────────────────
  listCollectiveExercises(): Observable<CollectiveExercise[]> {
    return this.http.get<CollectiveExercise[]>(`${this.base}/collective-exercises`);
  }
  getCollectiveExercise(id: string): Observable<CollectiveExerciseDetail> {
    return this.http.get<CollectiveExerciseDetail>(`${this.base}/collective-exercises/${id}`);
  }
  createCollectiveExercise(body: { name: string; range_id: string; objectives?: any[] }):
    Observable<{ id: string; name: string; objectives_added: number }> {
    return this.http.post<{ id: string; name: string; objectives_added: number }>(
      `${this.base}/collective-exercises`, body,
    );
  }
  importCollectiveObjectivesCsv(id: string, file: File): Observable<{ objectives_added: number }> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<{ objectives_added: number }>(
      `${this.base}/collective-exercises/${id}/objectives/import`, form,
    );
  }
  /** Replaces the whole MESL — confirm with the author before calling. */
  importMeslCsv(id: string, file: File): Observable<{ serials: number }> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<{ serials: number }>(`${this.base}/collective-exercises/${id}/mesl/import`, form);
  }
  /** Also replaces the whole MESL. */
  generateMesl(id: string, body: { event_count?: number; adversary?: string; duration_days?: number }):
    Observable<{ serials: number; model_used: string }> {
    return this.http.post<{ serials: number; model_used: string }>(
      `${this.base}/collective-exercises/${id}/mesl/generate`, body,
    );
  }
  patchMeslEvent(exerciseId: string, eventId: string, patch: Partial<MeslEvent>): Observable<MeslEvent> {
    return this.http.patch<MeslEvent>(
      `${this.base}/collective-exercises/${exerciseId}/mesl/${eventId}`, patch,
    );
  }

  // ── Exercises ────────────────────────────────────────────
  listExercises(limit = 50, offset = 0): Observable<ExerciseSummary[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<ExerciseSummary[]>(`${this.base}/exercises`, { params });
  }
  getExercise(id: string): Observable<Exercise> {
    return this.http.get<Exercise>(`${this.base}/exercises/${id}`);
  }
  createExercise(data: { name: string; range_id: string; scenario_id: string }): Observable<Exercise> {
    return this.http.post<Exercise>(`${this.base}/exercises`, data);
  }
  updateExercise(id: string, data: Partial<Exercise>): Observable<Exercise> {
    return this.http.put<Exercise>(`${this.base}/exercises/${id}`, data);
  }
  startExercise(id: string): Observable<Exercise> {
    return this.http.post<Exercise>(`${this.base}/exercises/${id}/start`, {});
  }
  runExercise(id: string): Observable<Exercise> {
    return this.http.post<Exercise>(`${this.base}/exercises/${id}/run`, {});
  }
  pauseExercise(id: string): Observable<Exercise> {
    return this.http.post<Exercise>(`${this.base}/exercises/${id}/pause`, {});
  }
  completeExercise(id: string): Observable<Exercise> {
    return this.http.post<Exercise>(`${this.base}/exercises/${id}/complete`, {});
  }

  // ── Objectives ───────────────────────────────────────────
  listObjectives(exerciseId: string): Observable<Objective[]> {
    return this.http.get<Objective[]>(`${this.base}/exercises/${exerciseId}/objectives`);
  }
  /** What each inject did (timeline and instructor), oldest first. */
  listInjects(exerciseId: string): Observable<InjectRecord[]> {
    return this.http.get<InjectRecord[]>(`${this.base}/exercises/${exerciseId}/injects`);
  }
  ackObjective(exerciseId: string, refId: string, evidence = ''): Observable<Objective> {
    return this.http.post<Objective>(
      `${this.base}/exercises/${exerciseId}/objectives/${refId}/ack`,
      { evidence }
    );
  }

  // ── Detections (ADR 0005) ────────────────────────────────
  /** Submit a Lucene detection for an objective; credited only if it finds the attack. */
  submitDetection(exerciseId: string, refId: string, query: string): Observable<Detection> {
    const body: components['schemas']['DetectionIn'] = { query };
    return this.http.post<Detection>(
      `${this.base}/exercises/${exerciseId}/objectives/${encodeURIComponent(refId)}/detections`, body,
    );
  }
  /** A Student's own attempts; every attempt (with on_target/precision) for staff. */
  listDetections(exerciseId: string): Observable<Detection[]> {
    return this.http.get<Detection[]>(`${this.base}/exercises/${exerciseId}/detections`);
  }

  // ── AAR ──────────────────────────────────────────────────
  generateAAR(exerciseId: string): Observable<AAR> {
    return this.http.post<AAR>(`${this.base}/exercises/${exerciseId}/aar/generate`, {});
  }
  getAAR(exerciseId: string): Observable<AAR> {
    return this.http.get<AAR>(`${this.base}/exercises/${exerciseId}/aar`);
  }
  getAARHtml(exerciseId: string): Observable<string> {
    return this.http.get(`${this.base}/exercises/${exerciseId}/aar/html`, { responseType: 'text' });
  }
  getAARPdf(exerciseId: string): Observable<Blob> {
    return this.http.get(`${this.base}/exercises/${exerciseId}/aar/pdf`, { responseType: 'blob' });
  }

  // ── Teams ────────────────────────────────────────────────
  listTeams(): Observable<Team[]> {
    return this.http.get<Team[]>(`${this.base}/teams`);
  }
  createTeam(data: { name: string }): Observable<Team> {
    return this.http.post<Team>(`${this.base}/teams`, data);
  }
  updateTeam(id: string, data: Partial<Team>): Observable<Team> {
    return this.http.patch<Team>(`${this.base}/teams/${id}`, data);
  }
  deleteTeam(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/teams/${id}`);
  }

  // ── Users ────────────────────────────────────────────────
  listUsers(): Observable<User[]> {
    return this.http.get<User[]>(`${this.base}/users`);
  }
  getMe(): Observable<User> {
    return this.http.get<User>(`${this.base}/users/me`);
  }
  updateUser(id: string, data: Partial<User>): Observable<User> {
    return this.http.patch<User>(`${this.base}/users/${id}`, data);
  }
  createUser(data: components['schemas']['UserCreateIn']): Observable<User> {
    return this.http.post<User>(`${this.base}/users`, data);
  }
  deleteUser(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/users/${id}`);
  }

  // ── Audit Log ────────────────────────────────────────────
  listAuditLog(limit = 100, offset = 0): Observable<unknown[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<unknown[]>(`${this.base}/audit-log`, { params });
  }

  // ── Telemetry ────────────────────────────────────────────
  searchTelemetry(rangeId: string, query = '*', size = 50): Observable<{ hits: { hits: { _source: TelemetryEvent }[] } }> {
    const params = new HttpParams().set('q', query).set('size', size);
    return this.http.get<{ hits: { hits: { _source: TelemetryEvent }[] } }>(`${this.base}/telemetry/${rangeId}/search`, { params });
  }
  ingestEvents(rangeId: string, events: TelemetryEvent[]): Observable<{ accepted: number }> {
    return this.http.post<{ accepted: number }>(`${this.base}/telemetry/${rangeId}/events`, events);
  }

  // ── Hypervisor inventory (vSphere) ───────────────────────
  /** Every discovered host, as of its connection's last discovery. Contacts no hypervisor. */
  hypervisorNodes(): Observable<HypervisorNode[]> {
    return this.http.get<HypervisorNode[]>(`${this.base}/hypervisors/nodes`);
  }

  // ── Proxmox Cluster (legacy) ─────────────────────────────
  proxmoxPing(): Observable<any> {
    return this.http.get<any>(`${this.base}/proxmox/ping`);
  }
  proxmoxDiscover(): Observable<any> {
    return this.http.get<any>(`${this.base}/proxmox/discover`);
  }
  proxmoxNodes(): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/proxmox/nodes`);
  }
  proxmoxNodeVms(node: string, includeTemplates = false): Observable<any[]> {
    const params = new HttpParams().set('include_templates', includeTemplates);
    return this.http.get<any[]>(`${this.base}/proxmox/nodes/${node}/vms`, { params });
  }
  proxmoxNodeNetworks(node: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/proxmox/nodes/${node}/networks`);
  }
  proxmoxNodeStorage(node: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/proxmox/nodes/${node}/storage`);
  }
  proxmoxTemplates(): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/proxmox/templates`);
  }
  proxmoxNextVmid(): Observable<{ vmid: number }> {
    return this.http.get<{ vmid: number }>(`${this.base}/proxmox/next-vmid`);
  }
  proxmoxClusterResources(type?: string): Observable<any[]> {
    let params = new HttpParams();
    if (type) params = params.set('resource_type', type);
    return this.http.get<any[]>(`${this.base}/proxmox/cluster/resources`, { params });
  }

  // ── Proxmox VM Lifecycle ─────────────────────────────────
  proxmoxCloneTemplate(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/clone`, data);
  }
  proxmoxCreateVm(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms`, data);
  }
  proxmoxGetVm(node: string, vmid: number): Observable<any> {
    return this.http.get<any>(`${this.base}/proxmox/vms/${node}/${vmid}`);
  }
  proxmoxUpdateVm(node: string, vmid: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.base}/proxmox/vms/${node}/${vmid}`, data);
  }
  proxmoxDestroyVm(node: string, vmid: number): Observable<any> {
    return this.http.delete<any>(`${this.base}/proxmox/vms/${node}/${vmid}`);
  }
  proxmoxStartVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/start`, {});
  }
  proxmoxStopVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/stop`, {});
  }
  proxmoxShutdownVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/shutdown`, {});
  }
  proxmoxResetVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/reset`, {});
  }
  proxmoxSuspendVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/suspend`, {});
  }
  proxmoxResumeVm(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/resume`, {});
  }
  proxmoxVncProxy(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/vnc`, {});
  }
  proxmoxSpiceProxy(node: string, vmid: number): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/spice`, {});
  }

  // ── Proxmox Snapshots ───────────────────────────────────
  proxmoxListSnapshots(node: string, vmid: number): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/proxmox/vms/${node}/${vmid}/snapshots`);
  }
  proxmoxCreateSnapshot(node: string, vmid: number, data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/snapshots`, data);
  }
  proxmoxRollbackSnapshot(node: string, vmid: number, snap: string): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/vms/${node}/${vmid}/snapshots/${snap}/rollback`, {});
  }
  proxmoxDeleteSnapshot(node: string, vmid: number, snap: string): Observable<any> {
    return this.http.delete<any>(`${this.base}/proxmox/vms/${node}/${vmid}/snapshots/${snap}`);
  }

  // ── Proxmox Networking ──────────────────────────────────
  proxmoxCreateBridge(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/networks`, data);
  }
  proxmoxApplyNetwork(node: string): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/networks/${node}/apply`, {});
  }
  proxmoxDeleteNetwork(node: string, iface: string): Observable<any> {
    return this.http.delete<any>(`${this.base}/proxmox/networks/${node}/${iface}`);
  }

  // ── Proxmox Storage & ISOs ──────────────────────────────
  proxmoxStorageContent(node: string, storage: string, contentType?: string): Observable<any[]> {
    let params = new HttpParams();
    if (contentType) params = params.set('content_type', contentType);
    return this.http.get<any[]>(`${this.base}/proxmox/storage/${node}/${storage}/content`, { params });
  }
  proxmoxListIsos(node: string, storage = 'local'): Observable<any[]> {
    const params = new HttpParams().set('storage', storage);
    return this.http.get<any[]>(`${this.base}/proxmox/isos/${node}`, { params });
  }

  // ── Proxmox Task Tracking ──────────────────────────────
  proxmoxTaskStatus(node: string, upid: string): Observable<any> {
    return this.http.get<any>(`${this.base}/proxmox/tasks/${node}/${encodeURIComponent(upid)}`);
  }
  proxmoxListTasks(node: string, limit = 20): Observable<any[]> {
    const params = new HttpParams().set('limit', limit);
    return this.http.get<any[]>(`${this.base}/proxmox/tasks/${node}`, { params });
  }

  // ── Proxmox Batch Deploy ───────────────────────────────
  proxmoxBatchDeploy(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/proxmox/deploy-batch`, data);
  }

  // ── Scheduling & Capacity ────────────────────────────────
  getCapacity(start?: string, end?: string): Observable<any> {
    let params = new HttpParams();
    if (start) params = params.set('start_time', start);
    if (end) params = params.set('end_time', end);
    return this.http.get<any>(`${this.base}/schedule/capacity`, { params });
  }

  // ── Curriculum Forge ─────────────────────────────────────
  listCurricula(): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/curricula`);
  }
  createCurriculum(data: { name: string; description?: string }): Observable<any> {
    return this.http.post<any>(`${this.base}/curricula`, data);
  }
  getCurriculum(id: string): Observable<any> {
    return this.http.get<any>(`${this.base}/curricula/${id}`);
  }
  deleteCurriculum(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/curricula/${id}`);
  }
  uploadCurriculumDocuments(id: string, files: File[]): Observable<any> {
    const form = new FormData();
    files.forEach(f => form.append('files', f, f.name));
    return this.http.post<any>(`${this.base}/curricula/${id}/documents`, form);
  }
  addCurriculumUrls(id: string, urls: string[]): Observable<any> {
    return this.http.post<any>(`${this.base}/curricula/${id}/urls`, { urls });
  }
  searchCurriculum(id: string, query: string, k = 8): Observable<any[]> {
    return this.http.post<any[]>(`${this.base}/curricula/${id}/search`, { query, k });
  }
  generateCourseFromCurriculum(id: string, data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/curricula/${id}/generate-course`, data);
  }

  // ── Quizzes ──────────────────────────────────────────────
  listQuizzes(curriculumId?: string): Observable<any[]> {
    const params = curriculumId ? new HttpParams().set('curriculum_id', curriculumId) : undefined;
    return this.http.get<any[]>(`${this.base}/quizzes`, { params });
  }
  getQuiz(id: string): Observable<any> {
    return this.http.get<any>(`${this.base}/quizzes/${id}`);
  }
  getQuizQuestions(id: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/quizzes/${id}/questions`);
  }
  updateQuiz(id: string, data: any): Observable<any> {
    return this.http.patch<any>(`${this.base}/quizzes/${id}`, data);
  }
  generateQuiz(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/quizzes/generate`, data);
  }
  startQuizAttempt(quizId: string): Observable<any> {
    return this.http.post<any>(`${this.base}/quizzes/${quizId}/attempts`, {});
  }
  submitQuizAttempt(attemptId: string, answers: Record<string, number[]>): Observable<any> {
    return this.http.post<any>(`${this.base}/quizzes/attempts/${attemptId}/submit`, { answers });
  }
  quizExportUrl(quizId: string, format: 'gift' | 'moodlexml'): string {
    return `${this.base}/quizzes/${quizId}/export?format=${format}`;
  }

  // ── Adaptive learning / competency profile ──────────────
  getCompetencyProfile(userId: string): Observable<any> {
    return this.http.get<any>(`${this.base}/competency/users/${userId}/profile`);
  }
  /** GET /competency/heatmap (untyped in the contract). */
  getCompetencyHeatmap(view: string): Observable<CompetencyHeatmap> {
    return this.http.get<CompetencyHeatmap>(`${this.base}/competency/heatmap`, { params: { view } });
  }
}
