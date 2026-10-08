import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';
import { Observable, from, map } from 'rxjs';
import { AuthService } from '../services/auth.service';
import { safeReturnUrl } from '../auth/return-url';

/**
 * Guards resolve asynchronously.
 *
 * Identity comes from `GET /auth/me`, which is a network call, so a guard can
 * run before it has landed. Reading the signal synchronously would bounce a
 * legitimately logged-in user to /login on a hard refresh. Every guard here
 * therefore awaits `bootstrap()` first.
 */
function resolved<T>(fn: () => T): Observable<T> {
  const auth = inject(AuthService);
  return from(auth.bootstrap()).pipe(map(() => fn()));
}

/**
 * Requires a fully registered, approved account.
 *
 * An anonymous visitor goes to /login with the URL they tried as `returnUrl`, so signing
 * in brings them back to it. Only a same-origin relative path is passed on (safeReturnUrl);
 * the login page validates it again before using it.
 */
export const authGuard: CanActivateFn = (_route, state): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const attempted = safeReturnUrl(state?.url);
  const returnUrl = attempted && attempted !== '/' ? attempted : null; // root needs no return

  return resolved<boolean | UrlTree>(() => {
    if (auth.isAuthenticated()) {
      return true;
    }
    // Authenticated with AD but no account yet: send them to the right place
    // rather than to a login page they have already passed.
    switch (auth.authState()) {
      case 'unregistered':
        return router.createUrlTree(['/register']);
      case 'pending':
      case 'rejected':
        return router.createUrlTree(['/registration-pending']);
      default:
        return router.createUrlTree(['/login'], returnUrl ? { queryParams: { returnUrl } } : {});
    }
  });
};

/**
 * The registration form is only for identities that do not yet have an account.
 * Someone already registered would otherwise be able to submit a second request.
 */
export const registrationGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() => {
    const state = auth.authState();
    if (state === 'unregistered') {
      return true;
    }
    if (state === 'pending' || state === 'rejected') {
      return router.createUrlTree(['/registration-pending']);
    }
    if (auth.isAuthenticated()) {
      return router.createUrlTree(['/dashboard']);
    }
    return router.createUrlTree(['/login']);
  });
};

/** The waiting room, for a submitted-but-undecided request. */
export const pendingGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() => {
    const state = auth.authState();
    if (state === 'pending' || state === 'rejected') {
      return true;
    }
    if (state === 'unregistered') {
      return router.createUrlTree(['/register']);
    }
    if (auth.isAuthenticated()) {
      return router.createUrlTree(['/dashboard']);
    }
    return router.createUrlTree(['/login']);
  });
};

/**
 * Holds a trainee in first-run until they have completed it.
 *
 * Scoped to `student` inside `needsOnboarding` — an administrator's first login
 * must not be trapped behind a trainee profile wizard.
 */
export const onboardingGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() =>
    auth.needsOnboarding() ? router.createUrlTree(['/onboarding']) : true,
  );
};

export const adminGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() =>
    auth.isAdmin() ? true : router.createUrlTree(['/dashboard']),
  );
};

export const instructorGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() =>
    auth.isInstructor() ? true : router.createUrlTree(['/dashboard']),
  );
};

/** The scheduler is for staff: every role except Student (ADR 0004). */
export const scheduleGuard: CanActivateFn = (): Observable<boolean | UrlTree> => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return resolved<boolean | UrlTree>(() =>
    auth.canViewSchedule() ? true : router.createUrlTree(['/dashboard']),
  );
};
