import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, from, map } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { rangeCapabilities } from './range-capabilities';

/**
 * The range recovery screen is for staff who hold range:read on a range's operations:
 * admin, instructor, range_ops, and observers (read-only). Students see their own labs
 * elsewhere. The API checks every call regardless; this only keeps the screen honest.
 */
export const rangeStaffGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return from(auth.bootstrap()).pipe(
    map(() => (rangeCapabilities(auth.user()?.role).view ? true : router.createUrlTree(['/dashboard']))),
  );
};
