import { APP_INITIALIZER, ApplicationConfig, importProvidersFrom } from '@angular/core';
import { provideRouter, withComponentInputBinding, withPreloading, PreloadAllModules } from '@angular/router';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideLottieOptions } from 'ngx-lottie';
import { KeycloakAngularModule, KeycloakService } from 'keycloak-angular';
import { routes } from './app.routes';
import { authInterceptorProvider } from './core/interceptors/auth.interceptor';
import { initializeKeycloak } from './core/auth/keycloak-init';

export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(routes, withComponentInputBinding(), withPreloading(PreloadAllModules)),
    provideHttpClient(withInterceptorsFromDi()),
    provideAnimations(),
    // lottie-web loads as its own lazy chunk, only when an animation renders.
    provideLottieOptions({ player: () => import('lottie-web') }),
    // keycloak-angular 15 is APP_INITIALIZER-shaped, which works unchanged
    // inside a standalone ApplicationConfig.
    importProvidersFrom(KeycloakAngularModule),
    KeycloakService,
    {
      provide: APP_INITIALIZER,
      useFactory: initializeKeycloak,
      multi: true,
      deps: [KeycloakService],
    },
    authInterceptorProvider,
  ],
};
