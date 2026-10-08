import type { AppEnvironment } from '@env/app-environment';
import { environment as prodEnvironment } from '@env/environment.prod';
import { RUNTIME_CONFIG_URL, applyRuntimeConfig, loadRuntimeConfig } from './runtime-config';

function env(): AppEnvironment {
  return {
    production: true,
    apiUrl: '/api',
    keycloak: { url: '/auth', realm: 'truenorth', clientId: 'truenorth-web' },
    authDisabled: false,
  };
}

function respond(status: number, body: unknown): typeof fetch {
  return () => Promise.resolve(new Response(JSON.stringify(body), { status }));
}

describe('production environment', () => {
  // The bundle itself is checked by scripts/check-prod-env.mjs after `ng build`.
  it('keeps authentication on', () => {
    expect(prodEnvironment.authDisabled).toBeFalse();
    expect(prodEnvironment.production).toBeTrue();
  });
});

describe('runtime config', () => {
  it('overrides the Keycloak endpoint', () => {
    const e = env();
    applyRuntimeConfig({ keycloak: { url: 'https://sso.example.com/auth', realm: 'r', clientId: 'c' } }, e);
    expect(e.keycloak).toEqual({ url: 'https://sso.example.com/auth', realm: 'r', clientId: 'c' });
  });

  it('never turns authentication off', () => {
    const e = env();
    applyRuntimeConfig({ authDisabled: true, keycloak: { authDisabled: true } }, e);
    expect(e.authDisabled).toBeFalse();
  });

  it('ignores other top-level fields, blanks and non-strings', () => {
    const e = env();
    applyRuntimeConfig({ apiUrl: 'https://evil.example', keycloak: { url: '  ', realm: 7, clientId: null } }, e);
    expect(e).toEqual(env());
  });

  it('keeps the defaults for {} and for non-objects', () => {
    for (const body of [{}, null, 'x', 1, [], { keycloak: 'x' }]) {
      const e = env();
      applyRuntimeConfig(body, e);
      expect(e).toEqual(env());
    }
  });

  it('loads assets/config.json without caching', async () => {
    const e = env();
    const fetchFn = jasmine.createSpy('fetch').and.callFake(respond(200, { keycloak: { url: 'http://localhost:18180' } }));
    await loadRuntimeConfig(fetchFn, e);
    expect(fetchFn).toHaveBeenCalledWith(RUNTIME_CONFIG_URL, jasmine.objectContaining({ cache: 'no-store' }));
    expect(e.keycloak.url).toBe('http://localhost:18180');
  });

  it('falls back to the build defaults when the file is missing or unreadable', async () => {
    spyOn(console, 'warn');
    const missing = env();
    await loadRuntimeConfig(respond(404, {}), missing);
    expect(missing).toEqual(env());

    const broken = env();
    await loadRuntimeConfig(() => Promise.reject(new TypeError('network')), broken);
    expect(broken).toEqual(env());

    const garbled = env();
    await loadRuntimeConfig(() => Promise.resolve(new Response('not json', { status: 200 })), garbled);
    expect(garbled).toEqual(env());
    expect(console.warn).toHaveBeenCalledTimes(3);
  });
});
