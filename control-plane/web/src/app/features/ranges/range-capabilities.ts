import { HttpErrorResponse } from '@angular/common/http';
import type { UserRole } from '@core/services/auth.service';

/**
 * What the range recovery screen offers each role. A mirror of control-plane/api/app/rbac.py
 * for display only: the API checks every call, and a refusal is shown as such.
 *
 * - view (range:read, staff only on this screen): admin, instructor, range_ops, observer
 * - abandon an operation, snapshots (range:provision / range:destroy): admin, instructor, range_ops
 * - force-release a lease (range:lease_force_release): admin
 * - abandon a range: a platform administrator (an admin of the operator's tenant; the API decides)
 */
export interface RangeCapabilities {
  view: boolean;
  changeRange: boolean;
  forceRelease: boolean;
  abandonRange: boolean;
}

export function rangeCapabilities(role: UserRole | null | undefined): RangeCapabilities {
  const staff = role === 'admin' || role === 'instructor' || role === 'range_ops';
  return {
    view: staff || role === 'observer',
    changeRange: staff,
    forceRelease: role === 'admin',
    abandonRange: role === 'admin',
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
