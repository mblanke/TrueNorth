import { Injectable, inject } from '@angular/core';
import {
  HttpInterceptor,
  HttpRequest,
  HttpHandler,
  HttpEvent,
  HTTP_INTERCEPTORS,
} from '@angular/common/http';
import { Observable, from, switchMap } from 'rxjs';
import { KeycloakService } from 'keycloak-angular';
import { environment } from '@env/environment';

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : null;
}

@Injectable()
export class AuthInterceptor implements HttpInterceptor {
  private readonly keycloak = inject(KeycloakService);

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
    return from(this.keycloak.getToken()).pipe(
      switchMap((token) => {
        const headers = { ...csrfHeaders };
        if (token) {
          headers['Authorization'] = `Bearer ${token}`;
        }
        return next.handle(
          Object.keys(headers).length ? req.clone({ setHeaders: headers }) : req,
        );
      }),
    );
  }
}

export const authInterceptorProvider = {
  provide: HTTP_INTERCEPTORS,
  useClass: AuthInterceptor,
  multi: true,
};
