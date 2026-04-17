import { Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import {
  AAR, Exercise, HealthResponse, Objective, Range,
  Scenario, Team, Template, Tenant, TelemetryEvent, User,
} from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = environment.apiUrl;

  constructor(private http: HttpClient) {}

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
  deleteTenant(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/tenants/${id}`);
  }

  // ── Templates ────────────────────────────────────────────
  listTemplates(limit = 50, offset = 0): Observable<Template[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<Template[]>(`${this.base}/templates`, { params });
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
  listScenarios(limit = 50, offset = 0): Observable<Scenario[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<Scenario[]>(`${this.base}/scenarios`, { params });
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
  listRanges(limit = 50, offset = 0): Observable<Range[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<Range[]>(`${this.base}/ranges`, { params });
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

  // ── Exercises ────────────────────────────────────────────
  listExercises(limit = 50, offset = 0): Observable<Exercise[]> {
    const params = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<Exercise[]>(`${this.base}/exercises`, { params });
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
  ackObjective(exerciseId: string, refId: string, evidence = ''): Observable<Objective> {
    return this.http.post<Objective>(
      `${this.base}/exercises/${exerciseId}/objectives/${refId}/ack`,
      { evidence }
    );
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

  // ── Proxmox Cluster ───────────────────────────────────────
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
  checkCapacity(body: { start_time: string; end_time: string; vcpu_needed: number; ram_mb_needed: number; disk_gb_needed: number }): Observable<any> {
    return this.http.post<any>(`${this.base}/schedule/check`, body);
  }
  listScheduledEvents(state?: string, limit = 50): Observable<any> {
    let params = new HttpParams().set('limit', limit);
    if (state) params = params.set('state', state);
    return this.http.get<any>(`${this.base}/schedule/events`, { params });
  }
  createScheduledEvent(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/schedule/events`, data);
  }
  deleteScheduledEvent(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/schedule/events/${id}`);
  }
  updateScheduledEvent(id: string, data: any): Observable<any> {
    return this.http.put<any>(`${this.base}/schedule/events/${id}`, data);
  }
  getResourceTimeline(days = 7): Observable<any> {
    const params = new HttpParams().set('days', days);
    return this.http.get<any>(`${this.base}/schedule/timeline`, { params });
  }


  // ── Helpdesk / Support Tickets ───────────────────────────
  listQueues(): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/tickets/queues`);
  }
  createQueue(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets/queues`, data);
  }
  addQueueMember(queueId: string, userId: string): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets/queues/${queueId}/members`, { user_id: userId });
  }
  removeQueueMember(queueId: string, userId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/tickets/queues/${queueId}/members/${userId}`);
  }

  listTickets(params?: { status?: string; priority?: string; category?: string; queue_id?: string; assigned_to?: string }): Observable<any[]> {
    let httpParams = new HttpParams();
    if (params) {
      Object.entries(params).forEach(([k, v]) => { if (v) httpParams = httpParams.set(k, v); });
    }
    return this.http.get<any[]>(`${this.base}/tickets`, { params: httpParams });
  }
  createTicket(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets`, data);
  }
  getTicket(id: string): Observable<any> {
    return this.http.get<any>(`${this.base}/tickets/${id}`);
  }
  updateTicket(id: string, data: any): Observable<any> {
    return this.http.put<any>(`${this.base}/tickets/${id}`, data);
  }
  deleteTicket(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/tickets/${id}`);
  }

  listTicketComments(ticketId: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/tickets/${ticketId}/comments`);
  }
  addTicketComment(ticketId: string, data: { author_id: string; body: string }): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets/${ticketId}/comments`, data);
  }

  // AI Agent
  askAI(ticketId: string): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets/${ticketId}/ask-ai`, {});
  }
  runDiagnostics(ticketId: string): Observable<any> {
    return this.http.post<any>(`${this.base}/tickets/${ticketId}/run-diagnostics`, {});
  }
  listAIActions(ticketId: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/tickets/${ticketId}/ai-actions`);
  }

  // ── Wiki / Knowledge Base ────────────────────────────────
  listWikiSpaces(): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/wiki/spaces`);
  }
  createWikiSpace(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/wiki/spaces`, data);
  }
  getWikiSpace(slug: string): Observable<any> {
    return this.http.get<any>(`${this.base}/wiki/spaces/${slug}`);
  }
  deleteWikiSpace(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/wiki/spaces/${id}`);
  }

  listWikiPages(spaceSlug: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/wiki/spaces/${spaceSlug}/pages`);
  }
  getWikiPageTree(spaceSlug: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/wiki/spaces/${spaceSlug}/tree`);
  }
  createWikiPage(data: any): Observable<any> {
    return this.http.post<any>(`${this.base}/wiki/pages`, data);
  }
  getWikiPage(pageId: string): Observable<any> {
    return this.http.get<any>(`${this.base}/wiki/pages/${pageId}`);
  }
  updateWikiPage(pageId: string, data: any): Observable<any> {
    return this.http.put<any>(`${this.base}/wiki/pages/${pageId}`, data);
  }
  deleteWikiPage(pageId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/wiki/pages/${pageId}`);
  }
  listWikiPageRevisions(pageId: string): Observable<any[]> {
    return this.http.get<any[]>(`${this.base}/wiki/pages/${pageId}/revisions`);
  }
  searchWiki(q: string): Observable<any[]> {
    const params = new HttpParams().set('q', q);
    return this.http.get<any[]>(`${this.base}/wiki/search`, { params });
  }
}
