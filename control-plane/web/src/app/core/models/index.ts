// ── TrueNorth Range TypeScript Models ─────────────────────────────────
// Aligned with backend schemas (control-plane/api/app/schemas.py)

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  created_at: string;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: 'admin' | 'instructor' | 'student' | 'observer' | 'range_ops';
  tenant_id: string;
  is_active: boolean;
  created_at: string;
}

export interface Team {
  id: string;
  name: string;
  tenant_id: string;
  created_at: string;
}

export interface Template {
  id: string;
  name: string;
  version: string;
  yaml: string;
  is_public: boolean;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

export interface Scenario {
  id: string;
  name: string;
  version: string;
  yaml: string;
  is_public: boolean;
  tenant_id: string;
  created_at: string;
  updated_at: string;
}

// Matches backend RangeState enum exactly
export type RangeState =
  | 'created'
  | 'provisioning'
  | 'ready'
  | 'running'
  | 'stopped'
  | 'destroying'
  | 'destroyed'
  | 'failed';

export interface Range {
  id: string;
  name: string;
  state: RangeState;
  template_id: string;
  tenant_id: string;
  provisioner_backend: string | null;
  provisioner_output: string | null;
  error_message: string | null;
  /** Operator-facing markdown: what this range is for, how it is used, ROE. */
  description: string;
  created_at: string;
  updated_at: string;
}

/** A supporting file attached to a range. */
export interface RangeDocument {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
}

// Matches backend ExerciseState enum exactly
export type ExerciseState =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'cancelled';

export interface Exercise {
  id: string;
  name: string;
  state: ExerciseState;
  range_id: string;
  scenario_id: string;
  tenant_id: string;
  started_at: string | null;
  completed_at: string | null;
  total_score: number;
  max_score: number;
  created_at: string;
  updated_at: string;
}

// Matches backend ObjectiveType enum exactly
export type ObjectiveType = 'detection' | 'response' | 'deliverable';

export interface Objective {
  id: string;
  exercise_id: string;
  ref_id: string;
  objective_type: ObjectiveType;
  description: string;
  validator: string;
  points: number;
  achieved: boolean;
  evidence: string | null;
  achieved_at: string | null;
}

export interface AAR {
  id: string;
  exercise_id: string;
  report_json: string;
  report_html: string;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  version: string;
  app: string;
  db: boolean;
  redis: boolean;
}

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

export interface HypervisorConnection {
  id: string;
  name: string;
  hypervisor_type: HypervisorType;
  host: string;
  port: number;
  username: string;
  is_primary: boolean;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
}

export interface HypervisorNode {
  id: string;
  connection_id: string;
  node_name: string;
  status: string;
  cpu_cores: number;
  cpu_usage_pct: number;
  memory_total_gb: number;
  memory_used_gb: number;
  storage_total_gb: number;
  storage_used_gb: number;
  vm_count: number;
  uptime_seconds: number;
  last_polled_at: string | null;
}

export interface HypervisorPool {
  id: string;
  connection_id: string;
  pool_name: string;
  description: string | null;
  member_count: number;
}

export interface HypervisorSummary {
  total_connections: number;
  active_connections: number;
  total_nodes: number;
  online_nodes: number;
  total_cpu: number;
  total_memory_gb: number;
  total_storage_gb: number;
  total_vms: number;
  by_type: Record<string, number>;
}

// ── AI Models ─────────────────────────────────────────────────────
export type AIBackendType = 'ollama' | 'openai' | 'azure_openai' | 'vllm' | 'custom';

export interface AIBackendConfig {
  id: string;
  name: string;
  backend_type: AIBackendType;
  base_url: string;
  is_primary: boolean;
  is_active: boolean;
  max_concurrent: number;
  timeout_seconds: number;
  default_model: string | null;
  created_at: string;
}

export interface AIFleetNode {
  id: string;
  backend_id: string;
  node_name: string;
  status: string;
  gpu_model: string | null;
  gpu_vram_gb: number | null;
  loaded_models: string | null;
  requests_per_min: number;
  last_health_check: string | null;
}

export interface AIModelRoute {
  id: string;
  route_name: string;
  model_pattern: string;
  backend_id: string;
  priority: number;
  is_active: boolean;
}

export interface AIFleetSummary {
  total_backends: number;
  active_backends: number;
  total_fleet_nodes: number;
  online_nodes: number;
  total_model_routes: number;
  primary_backend: string | null;
}

// ── Directory Models ──────────────────────────────────────────────
export interface Nation {
  id: string;
  name: string;
  iso_alpha2: string;
  iso_alpha3: string;
  flag_emoji: string;
  is_nato: boolean;
  is_fvey: boolean;
  is_active: boolean;
}

export interface Coalition {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  is_active: boolean;
  created_at: string;
}

export interface CoalitionMembership {
  id: string;
  coalition_id: string;
  nation_id: string;
  joined_at: string;
}

export type OUType = 'root' | 'division' | 'branch' | 'unit' | 'team' | 'custom';

export interface OrganizationalUnit {
  id: string;
  name: string;
  slug: string;
  ou_type: OUType;
  parent_id: string | null;
  nation_id: string | null;
  children: OrganizationalUnit[];
}

export type SecurityGroupType = 'role' | 'access' | 'distribution' | 'clearance' | 'exercise' | 'custom';

export interface SecurityGroup {
  id: string;
  name: string;
  slug: string;
  group_type: SecurityGroupType;
  description: string | null;
  created_at: string;
}

export interface SecurityGroupMembership {
  id: string;
  group_id: string;
  user_id: string;
  added_at: string;
}

// ── Auth Zone Models ──────────────────────────────────────────────
export interface AuthZonePolicy {
  id: string;
  zone_name: string;
  description: string | null;
  clearance_required: string;
  allowed_methods: string;
  require_mfa: boolean;
  session_timeout_minutes: number;
  max_failed_attempts: number;
  is_active: boolean;
}

// ── Extended User/Team Models ─────────────────────────────────────
export type ClearanceLevel = 'unclassified' | 'protected' | 'confidential' | 'secret' | 'top_secret';
export type UserSource = 'local' | 'ad_sync' | 'ldap' | 'scim' | 'keycloak';
export type TeamType = 'red' | 'blue' | 'white' | 'purple' | 'green' | 'custom';

export interface UserFull {
  id: string;
  email: string;
  display_name: string;
  role: string;
  first_name: string | null;
  last_name: string | null;
  rank: string | null;
  service_branch: string | null;
  nation_id: string | null;
  clearance_level: ClearanceLevel;
  unit: string | null;
  callsign: string | null;
  avatar_url: string | null;
  source: UserSource;
  is_active: boolean;
  created_at: string;
  timezone: string;
}

export interface TeamFull {
  id: string;
  name: string;
  description: string | null;
  team_type: TeamType | null;
  color_hex: string | null;
  max_members: number | null;
  is_persistent: boolean;
  created_at: string;
}

export interface ADSyncStatus {
  connected: boolean;
  last_sync_at: string | null;
  users_synced: number;
  groups_synced: number;
  errors: string[];
}


// ── Storage Models ────────────────────────────────────────────
export type StorageProtocol = 'nfs' | 'iscsi' | 'fc' | 'nvme_of' | 'smb';

export interface StorageAppliance {
  id: string;
  name: string;
  vendor: string;
  model: string;
  management_ip: string;
  protocol: StorageProtocol;
  raw_capacity_tb: number;
  usable_capacity_tb: number;
  is_active: boolean;
  notes: string | null;
  created_at: string;
}

export interface StorageVolume {
  id: string;
  appliance_id: string;
  volume_name: string;
  size_gb: number;
  used_gb: number;
  protocol: StorageProtocol;
  mount_path: string | null;
  created_at: string;
}

export interface StorageSummary {
  total_appliances: number;
  active_appliances: number;
  total_raw_tb: number;
  total_usable_tb: number;
  total_volumes: number;
}

// ── Network Device Models ─────────────────────────────────────
export type NetworkDeviceRole = 'tor' | 'spine' | 'leaf' | 'firewall' | 'router' | 'oob';

export interface NetworkDevice {
  id: string;
  name: string;
  vendor: string;
  model: string;
  role: NetworkDeviceRole;
  management_ip: string;
  firmware_version: string | null;
  port_count: number;
  is_active: boolean;
  notes: string | null;
  created_at: string;
}

export interface NetworkSummary {
  total_devices: number;
  active_devices: number;
  by_role: Record<string, number>;
}

// ── Kit Definition Models ─────────────────────────────────────
export interface KitDefinition {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  compute_node_count: number;
  storage_appliance_count: number;
  network_device_count: number;
  created_at: string;
}

