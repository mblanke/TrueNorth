import { environment } from '@env/environment';
import type { AppEnvironment } from '@env/app-environment';

/**
 * Runtime configuration: one web image, any environment.
 *
 * The build bakes defaults into environment.prod.ts. At start-up the web container
 * writes assets/config.json from TN_KEYCLOAK_URL / TN_KEYCLOAK_REALM /
 * TN_KEYCLOAK_CLIENT_ID (docker/40-truenorth-runtime-config.sh), and main.ts applies it
 * here before Angular bootstraps, so provideKeycloak() sees the deployed values.
 *
 * Only the Keycloak endpoint is overridable. `authDisabled` is deliberately not:
 * whoever can edit a served JSON file must not be able to turn authentication off,
 * and a production bundle is checked to carry `authDisabled: false`
 * (scripts/check-prod-env.mjs).
 *
 * Relative URL, so it resolves against <base href>. Fetched with `fetch`, not
 * HttpClient: this runs before the injector exists.
 */
export const RUNTIME_CONFIG_URL = 'assets/config.json';

const KEYCLOAK_KEYS = ['url', 'realm', 'clientId'] as const;

/** Copy the recognised, non-empty string fields of `body` onto `env`. Everything else is ignored. */
export function applyRuntimeConfig(body: unknown, env: AppEnvironment = environment): void {
  if (!body || typeof body !== 'object') {
    return;
  }
  const keycloak = (body as { keycloak?: unknown }).keycloak;
  if (!keycloak || typeof keycloak !== 'object') {
    return;
  }
  for (const key of KEYCLOAK_KEYS) {
    const value = (keycloak as Record<string, unknown>)[key];
    if (typeof value === 'string' && value.trim() !== '') {
      env.keycloak[key] = value.trim();
    }
  }
}

/**
 * Fetch and apply assets/config.json. A missing or unreadable file keeps the build
 * defaults (with a console warning) rather than blocking the app: the defaults are a
 * working production configuration wherever Keycloak is served under /auth.
 */
export async function loadRuntimeConfig(
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
  env: AppEnvironment = environment,
): Promise<void> {
  try {
    const resp = await fetchFn(RUNTIME_CONFIG_URL, { cache: 'no-store', credentials: 'same-origin' });
    if (!resp.ok) {
      console.warn(`runtime config: ${RUNTIME_CONFIG_URL} answered ${resp.status}; using build defaults`);
      return;
    }
    applyRuntimeConfig(await resp.json(), env);
  } catch (err) {
    console.warn(`runtime config: could not load ${RUNTIME_CONFIG_URL}; using build defaults`, err);
  }
}
