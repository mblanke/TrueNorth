import { environment } from '@env/environment';
import type { AppEnvironment } from '@env/app-environment';

/**
 * Runtime configuration: one web image, any environment.
 *
 * The build bakes defaults into environment.prod.ts. At start-up the web container
 * generates the file served as assets/config.json from TN_KEYCLOAK_* and TN_ADMIN_*
 * (docker/40-truenorth-runtime-config.sh; nginx serves it from /tmp), and main.ts
 * applies it here before Angular bootstraps, so provideKeycloak() sees the deployed
 * values.
 *
 * Only the Keycloak endpoint and the Admin > Services links (`adminLinks`) are
 * overridable. `authDisabled` is deliberately not:
 * whoever can edit a served JSON file must not be able to turn authentication off,
 * and a production bundle is checked to carry `authDisabled: false`
 * (scripts/check-prod-env.mjs).
 *
 * Relative URL, so it resolves against <base href>. Fetched with `fetch`, not
 * HttpClient: this runs before the injector exists.
 */
export const RUNTIME_CONFIG_URL = 'assets/config.json';

const KEYCLOAK_KEYS = ['url', 'realm', 'clientId'] as const;
const ADMIN_LINK_KEYS = ['keycloak', 'minio', 'dashboards', 'ai'] as const;

/** A non-empty, trimmed string, or undefined. */
function text(obj: unknown, key: string): string | undefined {
  if (!obj || typeof obj !== 'object') {
    return undefined;
  }
  const value = (obj as Record<string, unknown>)[key];
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

/** Only absolute http(s) URLs or same-origin paths are usable as links. */
function isLinkUrl(value: string): boolean {
  return /^https?:\/\/[^\s]+$/i.test(value) || /^\/(?!\/)\S*$/.test(value);
}

/** Copy the recognised, non-empty string fields of `body` onto `env`. Everything else is ignored. */
export function applyRuntimeConfig(body: unknown, env: AppEnvironment = environment): void {
  if (!body || typeof body !== 'object') {
    return;
  }
  const { keycloak, adminLinks } = body as { keycloak?: unknown; adminLinks?: unknown };
  for (const key of KEYCLOAK_KEYS) {
    const value = text(keycloak, key);
    if (value !== undefined) {
      env.keycloak[key] = value;
    }
  }
  for (const key of ADMIN_LINK_KEYS) {
    const value = text(adminLinks, key);
    if (value !== undefined && isLinkUrl(value)) {
      env.adminLinks[key] = value;
    }
  }
}

export interface ServiceLink {
  name: string;
  url: string;
}

/**
 * Admin > Services cards. Keycloak's console defaults to `<keycloak.url>/admin/`; the
 * others appear only when configured, so production never links to a developer's
 * localhost.
 */
export function adminServiceLinks(env: AppEnvironment = environment): ServiceLink[] {
  const links = env.adminLinks;
  const keycloak = links.keycloak ?? `${env.keycloak.url.replace(/\/+$/, '')}/admin/`;
  const all: [string, string | undefined][] = [
    ['Keycloak', keycloak],
    ['MinIO Console', links.minio],
    ['OpenSearch Dashboards', links.dashboards],
    ['AI Orchestrator', links.ai],
  ];
  return all
    .filter((entry): entry is [string, string] => !!entry[1] && isLinkUrl(entry[1]))
    .map(([name, url]) => ({ name, url }));
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
