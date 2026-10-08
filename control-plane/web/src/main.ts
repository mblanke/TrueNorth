import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { buildAppConfig } from './app/app.config';
import { loadRuntimeConfig } from './app/core/config/runtime-config';

// Runtime config first: provideKeycloak() copies the Keycloak endpoint when the
// providers are built, so they are built only after assets/config.json is applied.
loadRuntimeConfig()
  .then(() => bootstrapApplication(AppComponent, buildAppConfig()))
  .catch((err) => console.error(err));
