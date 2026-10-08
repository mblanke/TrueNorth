#!/usr/bin/env node
// Fails if a production build would ship with authentication disabled.
//
//   node scripts/check-prod-env.mjs           source checks + the built bundle (npm run check:prod-env)
//   node scripts/check-prod-env.mjs --source  source checks only (no build needed)
//
// Run after `ng build --configuration production` (CI build-angular, DoD with DOD_WEB=1,
// and the web Dockerfile). Why it exists: angular.json's production configuration once
// had no fileReplacements, so every production image shipped environment.ts with
// `authDisabled: true` -- every visitor was "Dev Admin" and no bearer token was sent.
//
// Source checks: angular.json swaps environment.ts for environment.prod.ts in the
// production configuration, and environment.prod.ts says `authDisabled: false`.
// Bundle checks (dist/truenorth-range-web/browser): no `authDisabled` set to true in any
// emitted script, at least one set to false (proof the property survived minification,
// so the first check means something), and no environment object with the dev-only
// Keycloak URL.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const project = 'truenorth-range-web';
const sourceOnly = process.argv.includes('--source');
const errors = [];

// ── Source ────────────────────────────────────────────────────────────
const angular = JSON.parse(readFileSync(join(webRoot, 'angular.json'), 'utf8'));
const build = angular.projects?.[project]?.architect?.build;
const prod = build?.configurations?.production;
if (!prod) {
  errors.push(`angular.json: no production configuration for ${project}`);
} else {
  const swaps = prod.fileReplacements ?? [];
  const ok = swaps.some(
    (r) => r.replace === 'src/environments/environment.ts' && r.with === 'src/environments/environment.prod.ts',
  );
  if (!ok) {
    errors.push(
      'angular.json: production configuration does not replace src/environments/environment.ts ' +
        'with src/environments/environment.prod.ts, so the dev environment (authDisabled: true) would ship',
    );
  }
}
if (build?.defaultConfiguration !== 'production') {
  errors.push(`angular.json: build.defaultConfiguration is ${JSON.stringify(build?.defaultConfiguration)}, expected "production"`);
}

const prodEnvPath = join(webRoot, 'src/environments/environment.prod.ts');
const prodEnv = readFileSync(prodEnvPath, 'utf8');
const flags = [...prodEnv.matchAll(/\bauthDisabled\s*:\s*([^,\s}]+)/g)].map((m) => m[1]);
if (flags.length !== 1 || flags[0] !== 'false') {
  errors.push(`src/environments/environment.prod.ts: expected exactly one \`authDisabled: false\`, found ${JSON.stringify(flags)}`);
}

// ── Bundle ────────────────────────────────────────────────────────────
if (!sourceOnly) {
  const dist = join(webRoot, 'dist', project, 'browser');
  if (!existsSync(dist)) {
    errors.push(`${relative(webRoot, dist)} not found: run \`ng build --configuration production\` first (or pass --source)`);
  } else {
    const scripts = [];
    const walk = (dir) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (name.endsWith('.js')) scripts.push(path);
      }
    };
    walk(dist);
    let falseHits = 0;
    for (const path of scripts) {
      const text = readFileSync(path, 'utf8');
      const rel = relative(webRoot, path);
      for (const m of text.matchAll(/\bauthDisabled\s*:\s*(!0|!1|true|false)/g)) {
        if (m[1] === '!0' || m[1] === 'true') {
          errors.push(`${rel}: production bundle has authDisabled set to ${m[1]}`);
        } else {
          falseHits += 1;
        }
      }
      // environment.ts's `keycloak: { url: 'http://localhost:8180' }`. Matched as a `url:`
      // property, not the bare string: admin.component.ts links to it in a template.
      if (/\burl\s*:\s*["'`]http:\/\/localhost:8180/.test(text)) {
        errors.push(`${rel}: production bundle contains the development Keycloak URL (environment.ts was bundled)`);
      }
    }
    if (scripts.length === 0) {
      errors.push(`${relative(webRoot, dist)}: no .js files`);
    } else if (falseHits === 0) {
      errors.push(
        `${relative(webRoot, dist)}: no \`authDisabled\` value found in ${scripts.length} script(s); ` +
          'the check cannot tell what shipped (did minification rename or inline it?)',
      );
    }
  }
}

if (errors.length) {
  for (const e of errors) console.error(`check:prod-env: ${e}`);
  process.exit(1);
}
console.log(`check:prod-env: OK (${sourceOnly ? 'source' : 'source + bundle'})`);
