import { HttpErrorResponse } from '@angular/common/http';
import type { UserRole } from '@core/services/auth.service';
import { PAGE_ACCESS, roleCan, roleHasAccess } from '@core/auth/permissions';

/**
 * What the range recovery screen offers each role, read from the permission table
 * (core/auth/permissions.ts, a mirror of control-plane/api/app/rbac.py). Display only:
 * the API checks every call, and a refusal is shown as such.
 *
 * - view: PAGE_ACCESS.rangeRecovery, the same key the route's pageGuard checks
 * - abandon an operation, take/restore a snapshot: range:provision / range:destroy
 * - delete a snapshot: range:destroy
 * - force-release a lease: range:lease_force_release
 * - abandon a range: a platform administrator only (an admin of the operator's tenant), as
 *   GET /auth/me reports it (`platform_admin`); other admins would only get a 403
 * - emergency teardown, even inside a maintenance window: range:admin (ADR 0013 §7)
 */
export interface RangeCapabilities {
  view: boolean;
  /** Abandon an operation (range:destroy) and take or restore a snapshot (range:provision). */
  changeRange: boolean;
  deleteSnapshot: boolean;
  forceRelease: boolean;
  abandonRange: boolean;
  emergencyTeardown: boolean;
}

export function rangeCapabilities(role: UserRole | null | undefined, platformAdmin = false): RangeCapabilities {
  return {
    view: roleHasAccess(role, PAGE_ACCESS.rangeRecovery),
    changeRange: roleCan(role, 'range:provision') && roleCan(role, 'range:destroy'),
    deleteSnapshot: roleCan(role, 'range:destroy'),
    forceRelease: roleCan(role, 'range:lease_force_release'),
    abandonRange: role === 'admin' && platformAdmin,
    emergencyTeardown: roleCan(role, 'range:admin'),
  };
}

/** The API's own words for a refusal, never a guess. */
export function apiError(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    const detail = err.error?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map(d => d?.msg ?? String(d)).join('; ');
    if (err.status === 0) return 'The API could not be reached.';
    if (err.status === 403) return 'Your role does not allow this.';
    if (err.status === 404) return 'Not found (or not in your organisation).';
    return `The API answered ${err.status}.`;
  }
  return 'The request failed.';
}
