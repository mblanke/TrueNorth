import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, from, map } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';

/**
 * Who does what in the Wiki and Support sections. Mirrors ROLE_PERMISSIONS in
 * control-plane/api/app/rbac.py (WIKI_EDIT / TICKET_WORK: admin, instructor,
 * range_ops; WIKI_ADMIN / TICKET_ADMIN: admin). The API enforces it; this only
 * decides what to show.
 */
const STAFF: UserRole[] = ['admin', 'instructor', 'range_ops'];

export const isStaffRole = (role: UserRole | undefined | null): boolean => !!role && STAFF.includes(role);
export const isAdminRole = (role: UserRole | undefined | null): boolean => role === 'admin';

/** Staff-only route (range ops counts as staff); anyone else lands on `fallback`. */
export function kbStaffGuard(fallback: string): CanActivateFn {
  return (): Observable<boolean | UrlTree> => {
    const auth = inject(AuthService);
    const router = inject(Router);
    return from(auth.bootstrap()).pipe(
      map(() => (isStaffRole(auth.user()?.role) ? true : router.createUrlTree([fallback]))),
    );
  };
}
