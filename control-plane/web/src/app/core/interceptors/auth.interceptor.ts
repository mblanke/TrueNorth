import { Injectable, Injector, inject } from '@angular/core';
import {
  HttpErrorResponse,
  HttpInterceptor,
  HttpRequest,
  HttpHandler,
  HttpEvent,
  HTTP_INTERCEPTORS,
} from '@angular/common/http';
import { Router } from '@angular/router';
import { Observable, catchError, from, switchMap, throwError } from 'rxjs';
import Keycloak from 'keycloak-js';
import { freshToken } from '../auth/keycloak-init';
import { ltiSessionToken } from '../auth/lti-session';
import { isMfaRequired, requestMfaStepUp } from '../auth/mfa-step-up';
import { safeReturnUrl } from '../auth/return-url';
import { AuthService } from '../services/auth.service';
import { environment } from '@env/environment';

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : null;
}

@Injectable()
export class AuthInterceptor implements HttpInterceptor {
  private readonly keycloak = inject(Keycloak);
  private readonly router = inject(Router);
  // AuthService itself uses HttpClient: fetched when needed, not at construction.
  private readonly injector = inject(Injector);

  intercept(req: HttpRequest<unknown>, next: HttpHandler): Observable<HttpEvent<unknown>> {
    // CSRF: double-submit cookie. app/middleware.py sets `truenorth_csrf` and
    // enforces the matching header on unsafe methods, so this is not optional.
    const csrfHeaders: Record<string, string> = {};
    const csrfToken = getCookie('truenorth_csrf');
    if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(req.method)) {
      csrfHeaders['X-CSRF-Token'] = csrfToken;
    }

    const isAsset = req.url.startsWith('/assets') || req.url.includes('silent-check-sso');
    if (environment.authDisabled || isAsset) {
      return next.handle(
        Object.keys(csrfHeaders).length ? req.clone({ setHeaders: csrfHeaders }) : req,
      );
    }

    // The token comes from the Keycloak adapter, which refreshes it as needed.
    // This previously read localStorage.getItem('access_token') — a key nothing
    // in the app ever wrote, so every authenticated request went out bare.
    // Without a Keycloak sign-in, a Student launched from Moodle carries their LTI
    // session (core/auth/lti-session.ts); a Keycloak token always wins.
    return from(freshToken(this.keycloak)).pipe(
      switchMap((kcToken) => {
        const token = kcToken || ltiSessionToken();
        const headers = { ...csrfHeaders };
        if (token) {
          headers['Authorization'] = `Bearer ${token}`;
        }
        return next.handle(
          Object.keys(headers).length ? req.clone({ setHeaders: headers }) : req,
        );
      }),
      catchError((err: unknown) => {
        if (isMfaRequired(err)) {
          this.stepUp();
        } else if (isAcknowledgementRequired(err)) {
          this.toRulesOfUse();
        }
        return throwError(() => err);
      }),
    );
  }

  /**
   * A new version of the rules of use was published (or a year passed) mid-session: the
   * API refuses everything until it is signed. Go to the form, then back to this page.
   */
  private toRulesOfUse(): void {
    this.injector.get(AuthService).setAcknowledgementRequired(true);
    const here = this.router.url;
    if (here.startsWith('/rules-of-use')) {
      return;
    }
    const returnUrl = safeReturnUrl(here);
    void this.router.navigate(['/rules-of-use'], returnUrl && returnUrl !== '/' ? { queryParams: { returnUrl } } : {});
  }

  /**
   * The API wants a second factor for this role (403 mfa_required): sign in again asking
   * for acr "mfa" (core/auth/mfa-step-up.ts). Once per window; after that, or without a
   * Keycloak session to step up, explain instead of looping.
   */
  private stepUp(): void {
    const outcome = this.keycloak.authenticated ? requestMfaStepUp(this.keycloak) : 'exhausted';
    if (outcome === 'exhausted') {
      void this.router.navigate(['/mfa-required']);
    }
  }
}

/** 403 `acknowledgement_required`: the rules of use must be signed first (ADR 0014 §3). */
export function isAcknowledgementRequired(err: unknown): boolean {
  if (!(err instanceof HttpErrorResponse) || err.status !== 403) {
    return false;
  }
  const detail = (err.error as { detail?: unknown } | null)?.detail;
  return (
    typeof detail === 'object' && detail !== null && (detail as { code?: unknown }).code === 'acknowledgement_required'
  );
}

export const authInterceptorProvider = {
  provide: HTTP_INTERCEPTORS,
  useClass: AuthInterceptor,
  multi: true,
};
