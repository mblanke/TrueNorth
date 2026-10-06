// ── TrueNorth Range TypeScript Models ─────────────────────────────────
// Wire types are aliases of the published contract (docs/interfaces/openapi.json),
// generated into ../api/schema.d.ts by `npm run gen:api` (ADR 0002). Do not hand-copy a
// backend schema here: alias it. Only frontend-only shapes (UI enums the contract types as
// plain strings, WebSocket frames, OpenSearch documents, generics) stay hand-written.
import type { components } from '../api/schema';

type S = components['schemas'];

export type Tenant = S['TenantOut'];

/** GET /users, /users/me and PATCH /users/{id} all return UserFullOut. */
export type User = S['UserFullOut'];

export type Team = S['TeamFullOut'];

/** GET /templates/{id}. */
export type Template = S['TemplateOut'];
/** One row of GET /templates: no yaml, tenant_id or updated_at. */
export type TemplateSummary = S['TemplateListOut'];

/** GET /scenarios/{id}. */
export type Scenario = S['ScenarioOut'];
/** One row of GET /scenarios: no yaml, tenant_id or updated_at. */
export type ScenarioSummary = S['ScenarioListOut'];

// The backend RangeState enum. The contract types `state` as a plain string, so this
// union is a UI-side convenience, not a guarantee.
export type RangeState =
  | 'created'
  | 'provisioning'
  | 'ready'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'starting'
  | 'destroying'
  | 'destroyed'
  | 'failed';

/** GET /ranges/{id} and every range mutation. `description` is operator markdown. */
export type Range = S['RangeOut'];
/** One row of GET /ranges: no template_id, tenant_id or provisioner fields. */
export type RangeSummary = S['RangeListOut'];

/** A supporting file attached to a range. */
export type RangeDocument = S['RangeDocumentOut'];

// The backend ExerciseState enum; the contract types `state` as a plain string.
export type ExerciseState =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'cancelled';

/** GET /exercises/{id} and every exercise mutation. */
export type Exercise = S['ExerciseOut'];
/** One row of GET /exercises: no range_id, scenario_id, tenant_id or updated_at. */
export type ExerciseSummary = S['ExerciseListOut'];

// The backend ObjectiveType enum (also used by scenario YAML authoring); the contract
// types `objective_type` as a plain string.
export type ObjectiveType = 'detection' | 'response' | 'deliverable';

export type Objective = S['ObjectiveOut'];

/** Note: the timestamp is `generated_at`, and `report_html` may be null. */
export type AAR = S['AAROut'];

export type HealthResponse = S['HealthOut'];

/** Generic envelope. The contract has one concrete schema per item type
 *  (e.g. PaginatedResponse_CourseListOut_); this is the same shape, made generic. */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface TelemetryEvent {
  '@timestamp'?: string;
  timestamp?: string;
  event_type?: string;
  hostname?: string;
  source_ip?: string;
  dest_ip?: string;
  process_name?: string;
  query?: string;
  range_id?: string;
  tenant_id?: string;
  inject?: boolean;
  [key: string]: unknown;
}

export interface WebSocketMessage {
  event: string;
  resource_type: string;
  resource_id: string;
  state?: string;
  timestamp: string;
}

// ── Hypervisor Models ─────────────────────────────────────────────
export type HypervisorType = 'proxmox' | 'vsphere' | 'hyperv';

export type HypervisorConnection = S['HypervisorConnectionOut'];

/** GET /hypervisors/nodes and /hypervisors/connections/:id/nodes (HypervisorNodeOut).
 *  null/absent means the hypervisor does not report that figure (vCenter's REST API gives
 *  no host CPU or memory) — show it as unknown, never as 0. `cpu_used` is percent busy. */
export type HypervisorNode = S['HypervisorNodeOut'];

export type HypervisorPool = S['HypervisorPoolOut'];

export type HypervisorSummary = S['HypervisorSummaryOut'];

// ── AI Models ─────────────────────────────────────────────────────
export type AIBackendType = 'ollama' | 'openai' | 'azure_openai' | 'vllm' | 'custom';

export type AIBackendConfig = S['AIBackendConfigOut'];

export type AIFleetNode = S['AIFleetNodeOut'];

export type AIModelRoute = S['AIModelRouteOut'];

export type AIFleetSummary = S['AIFleetSummaryOut'];

// ── Directory Models ──────────────────────────────────────────────
export type Nation = S['NationOut'];

export type Coalition = S['CoalitionOut'];

/** No contract schema: the API does not expose coalition memberships as objects. */
export interface CoalitionMembership {
  id: string;
  coalition_id: string;
  nation_id: string;
  joined_at: string;
}

export type OUType = 'root' | 'division' | 'branch' | 'unit' | 'team' | 'custom';

/** A node of the OU tree (OUTreeOut). */
export type OrganizationalUnit = S['OUTreeOut'];

export type SecurityGroupType = 'role' | 'access' | 'distribution' | 'clearance' | 'exercise' | 'custom';

export type SecurityGroup = S['SecurityGroupOut'];

/** No contract schema: membership is written via SecurityGroupMembershipIn only. */
export interface SecurityGroupMembership {
  id: string;
  group_id: string;
  user_id: string;
  added_at: string;
}

// ── Auth Zone Models ──────────────────────────────────────────────
export type AuthZonePolicy = S['AuthZonePolicyOut'];

// ── Extended User/Team Models ─────────────────────────────────────
export type ClearanceLevel = 'unclassified' | 'protected' | 'confidential' | 'secret' | 'top_secret';
export type UserSource = 'local' | 'ad_sync' | 'ldap' | 'scim' | 'keycloak';
export type TeamType = 'red' | 'blue' | 'white' | 'purple' | 'green' | 'custom';

export type UserFull = S['UserFullOut'];

export type TeamFull = S['TeamFullOut'];

/** GET /ad-sync/status has no response schema in the contract. */
export interface ADSyncStatus {
  connected: boolean;
  last_sync_at: string | null;
  users_synced: number;
  groups_synced: number;
  errors: string[];
}


// ── Storage Models ────────────────────────────────────────────
export type StorageProtocol = 'nfs' | 'iscsi' | 'fc' | 'nvme_of' | 'smb';

export type StorageAppliance = S['StorageApplianceOut'];

export type StorageVolume = S['StorageVolumeOut'];

export type StorageSummary = S['StorageSummaryOut'];

// ── Network Device Models ─────────────────────────────────────
export type NetworkDeviceRole = 'tor' | 'spine' | 'leaf' | 'firewall' | 'router' | 'oob';

export type NetworkDevice = S['NetworkDeviceOut'];

export type NetworkSummary = S['NetworkSummaryOut'];

// ── Kit Definition Models ─────────────────────────────────────
export type KitDefinition = S['KitDefinitionOut'];

