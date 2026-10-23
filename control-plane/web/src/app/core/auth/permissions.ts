/**
 * What each role may do, as the API decides it.
 *
 * A copy of `ROLE_PERMISSIONS` in control-plane/api/app/rbac.py, which is the authority:
 * every endpoint checks these tokens server-side. The menu, the route guards and the
 * buttons read this table so that a role is not shown a page or an action the API would
 * refuse. tests/api/test_web_permission_mirror.py fails when the two drift apart, so a
 * change to rbac.py is a change here in the same commit.
 *
 * Hiding is presentation, never access control: the API still refuses.
 */
import type { UserRole } from '../services/auth.service';

export type Permission =
  | 'range:create' | 'range:read' | 'range:update' | 'range:delete' | 'range:provision'
  | 'range:destroy' | 'range:batch_provision' | 'range:lease_force_release' | 'range:credentials_read'
  | 'range:credentials_rotate' | 'range:certification_read'
  | 'range:admin'
  | 'template:create' | 'template:read' | 'template:update' | 'template:delete'
  | 'scenario:create' | 'scenario:read' | 'scenario:update' | 'scenario:delete'
  | 'exercise:create' | 'exercise:read' | 'exercise:start' | 'exercise:complete' | 'exercise:pause'
  | 'detection:submit' | 'objective:ack'
  | 'user:create' | 'user:read' | 'user:update' | 'user:delete'
  | 'registration:read' | 'registration:approve'
  | 'infra:read' | 'infra:write' | 'infra:control'
  | 'ai_config:read' | 'ai_config:write'
  | 'integration:read' | 'integration:write'
  | 'learning_record:read' | 'learning_record:write'
  | 'enrollment:manage_own' | 'team:manage_own'
  | 'course:author' | 'course:release'
  | 'schedule:read' | 'schedule:write' | 'schedule:admin'
  | 'class:create' | 'class:read'
  | 'tenant:create' | 'tenant:read' | 'tenant:update'
  | 'audit:read' | 'telemetry:read' | 'telemetry:write' | 'stats:read' | 'aar:generate' | 'aar:read'
  | 'noise:read' | 'noise:control'
  | 'wiki:read' | 'wiki:edit' | 'wiki:admin'
  | 'ticket:create' | 'ticket:work' | 'ticket:admin';

/** Every permission (the admin role holds all of them, as `set(Permission)` does in rbac.py). */
export const ALL_PERMISSIONS: readonly Permission[] = [
  'range:create', 'range:read', 'range:update', 'range:delete', 'range:provision',
  'range:destroy', 'range:batch_provision', 'range:lease_force_release', 'range:credentials_read',
  'range:credentials_rotate', 'range:certification_read',
  'range:admin',
  'template:create', 'template:read', 'template:update', 'template:delete',
  'scenario:create', 'scenario:read', 'scenario:update', 'scenario:delete',
  'exercise:create', 'exercise:read', 'exercise:start', 'exercise:complete', 'exercise:pause',
  'detection:submit', 'objective:ack',
  'user:create', 'user:read', 'user:update', 'user:delete',
  'registration:read', 'registration:approve',
  'infra:read', 'infra:write', 'infra:control',
  'ai_config:read', 'ai_config:write',
  'integration:read', 'integration:write',
  'learning_record:read', 'learning_record:write',
  'enrollment:manage_own', 'team:manage_own',
  'course:author', 'course:release',
  'schedule:read', 'schedule:write', 'schedule:admin',
  'class:create', 'class:read',
  'tenant:create', 'tenant:read', 'tenant:update',
  'audit:read', 'telemetry:read', 'telemetry:write', 'stats:read', 'aar:generate', 'aar:read',
  'noise:read', 'noise:control',
  'wiki:read', 'wiki:edit', 'wiki:admin',
  'ticket:create', 'ticket:work', 'ticket:admin',
];

// mirror:start (read by tests/api/test_web_permission_mirror.py; one role per block)
export const ROLE_PERMISSIONS: Record<UserRole, readonly Permission[]> = {
  admin: ALL_PERMISSIONS,
  instructor: [
    'range:create', 'range:read', 'range:update', 'range:provision', 'range:destroy',
    'range:batch_provision', 'range:credentials_read', 'range:certification_read',
    'template:read', 'template:create',
    'scenario:create', 'scenario:read', 'scenario:update',
    'exercise:create', 'exercise:read', 'exercise:start', 'exercise:complete', 'exercise:pause',
    'detection:submit', 'objective:ack',
    'schedule:read', 'schedule:write',
    'class:create', 'class:read',
    'user:read',
    'registration:read', 'registration:approve',
    'infra:read', 'ai_config:read', 'integration:read',
    'learning_record:read', 'learning_record:write',
    'enrollment:manage_own', 'team:manage_own',
    'course:author', 'course:release',
    'stats:read', 'aar:generate', 'aar:read', 'telemetry:read', 'telemetry:write',
    'noise:read', 'noise:control',
    'wiki:read', 'wiki:edit', 'ticket:create', 'ticket:work',
  ],
  range_ops: [
    'infra:read', 'infra:write', 'infra:control', 'ai_config:read',
    'range:create', 'range:read', 'range:update', 'range:delete', 'range:provision',
    'range:destroy', 'range:batch_provision', 'range:credentials_read', 'range:certification_read',
    'template:read', 'template:create', 'template:update',
    'scenario:read', 'exercise:read', 'schedule:read', 'stats:read',
    'telemetry:read', 'telemetry:write',
    'wiki:read', 'wiki:edit', 'ticket:create', 'ticket:work',
  ],
  student: [
    'range:read', 'template:read', 'scenario:read', 'exercise:read', 'detection:submit',
    'aar:read', 'wiki:read', 'ticket:create',
  ],
  observer: [
    'range:read', 'template:read', 'scenario:read', 'exercise:read', 'aar:read',
    'schedule:read', 'class:read', 'telemetry:read', 'wiki:read',
  ],
};
// mirror:end

/** Whether `role` holds `permission`. No role (signed out) holds nothing. */
export function roleCan(role: UserRole | null | undefined, permission: Permission): boolean {
  return !!role && (ROLE_PERMISSIONS[role] ?? []).includes(permission);
}

/** Granted by an administrator only (STAFF_GRANTED_BY_ADMIN_ONLY in rbac.py). */
const STAFF_ROLES: readonly UserRole[] = ['admin', 'instructor', 'range_ops'];
const ROLE_ORDER: readonly UserRole[] = ['student', 'observer', 'instructor', 'range_ops', 'admin'];

/**
 * The roles `granter` may give someone, as `grant_refusal` in rbac.py decides: never a
 * staff role unless an admin, and never more authority than the granter holds. The
 * approval queue offers only these; the API refuses any other.
 */
export function grantableRoles(granter: UserRole | null | undefined): UserRole[] {
  if (!granter) return [];
  return ROLE_ORDER.filter(role =>
    (granter === 'admin' || !STAFF_ROLES.includes(role))
    && ROLE_PERMISSIONS[role].every(p => roleCan(granter, p)));
}

/**
 * What a page needs, for the menu and the route guards. `all` must all be held; with
 * `any`, at least one of them as well.
 */
export interface Access {
  all?: readonly Permission[];
  any?: readonly Permission[];
}

export function roleHasAccess(role: UserRole | null | undefined, access: Access | undefined): boolean {
  if (!access) return !!role;
  const all = access.all ?? [];
  const any = access.any ?? [];
  return all.every(p => roleCan(role, p)) && (any.length === 0 || any.some(p => roleCan(role, p)));
}

/**
 * The pages, and what each needs. One table for the menu, the route guards and the
 * dashboard's links, so a link is never shown to a role the guard would bounce.
 * Each entry names the API permission the page's own calls need.
 */
export const PAGE_ACCESS = {
  /** Range designer, ranges and templates (range_ops), scenarios, courses (instructors). */
  authoring: { any: ['range:create', 'template:create', 'scenario:create', 'course:author'] },
  authoringRanges: { any: ['range:create', 'range:update'] },
  authoringContent: { any: ['template:create', 'template:update'] },
  authoringScenarios: { all: ['scenario:create'] },
  /** routers/detection_rules.py and threat_intel.py write with range:update. */
  authoringDetections: { all: ['range:update'] },
  authoringThreatIntel: { all: ['range:update'] },
  /** MESL and the forge create exercises. */
  authoringExercises: { all: ['exercise:create'] },
  authoringCourses: { all: ['course:author'] },
  authoringNoise: { all: ['noise:read'] },
  /** Ops Center: live telemetry beside the exercise (white cell, observers, range ops). */
  opsCenter: { all: ['exercise:read', 'telemetry:read'] },
  /** Scoring and the AAR. Students read only reports of exercises they took part in (API). */
  scoring: { all: ['aar:read'] },
  telemetry: { all: ['telemetry:read'] },
  schedule: { all: ['schedule:read'] },
  /** GET /classes/mine: the staff Classes page (ADR 0014). Creating one needs class:create. */
  classes: { all: ['class:read'] },
  /** GET /maintenance-windows (schedule:read); changing them needs range:admin (admins). */
  maintenanceWindows: { all: ['schedule:read', 'range:admin'] },
  approvals: { all: ['registration:read'] },
  infrastructure: { all: ['infra:read'] },
  aiOrchestrator: { all: ['ai_config:read'] },
  support: { all: ['ticket:create'] },
  wiki: { all: ['wiki:read'] },
  /** A range's operations, lease, reservations and snapshots (R0-B): those who act on ranges. */
  rangeRecovery: { all: ['range:read'], any: ['range:provision', 'range:destroy'] },
  /** GET /teams/{id}/members; changing a team needs user:update (admins) or team:manage_own (its exercise is yours). */
  teams: { all: ['user:read'] },
  /** GET /courses/{id}/enrollments and /progress/{uid}; mark-complete needs learning_record:write. */
  learnerProgress: { all: ['learning_record:read'] },
} as const satisfies Record<string, Access>;

export type PageKey = keyof typeof PAGE_ACCESS;
