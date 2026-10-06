import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter, withComponentInputBinding, withPreloading, PreloadAllModules } from '@angular/router';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideLottieOptions } from 'ngx-lottie';
import { routes } from './app.routes';
import { authInterceptorProvider } from './core/interceptors/auth.interceptor';
import { provideTrueNorthKeycloak } from './core/auth/keycloak-init';

export const appConfig: ApplicationConfig = {
  providers: [
    // Angular 21 bootstraps zoneless by default; the app is written for zone.js.
    provideZoneChangeDetection(),
    provideRouter(routes, withComponentInputBinding(), withPreloading(PreloadAllModules)),
    provideHttpClient(withInterceptorsFromDi()),
    provideAnimations(),
    // lottie-web loads as its own lazy chunk, only when an animation renders.
    provideLottieOptions({ player: () => import('lottie-web') }),
    provideTrueNorthKeycloak(),
    authInterceptorProvider,
  ],
};
