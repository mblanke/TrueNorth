#!/usr/bin/env node
// Generates src/app/core/api/schema.d.ts from the published contract
// (docs/interfaces/openapi.json) with openapi-typescript. ADR 0002: clients are
// generated from the contract, not hand-written.
//
//   node scripts/api-types.mjs --write   regenerate in place   (npm run gen:api)
//   node scripts/api-types.mjs           fail if it is stale   (npm run check:api)
//
// --default-non-nullable false: a field is required only if the contract lists it in
// `required`. openapi-typescript's default also treats every field with a `default`
// as present, which is wrong for request bodies: FastAPI $refs them to
// components/schemas, so e.g. AIBackendConfigIn.is_primary would become mandatory.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FLAGS = ['--default-non-nullable', 'false'];

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const contract = resolve(webRoot, '../../docs/interfaces/openapi.json');
const committed = resolve(webRoot, 'src/app/core/api/schema.d.ts');
const bin = resolve(webRoot, 'node_modules/openapi-typescript/bin/cli.js');

function generate(out) {
  execFileSync(process.execPath, [bin, contract, ...FLAGS, '-o', out], {
    stdio: ['ignore', 'ignore', 'inherit'],
  });
}

if (process.argv.includes('--write')) {
  generate(committed);
  console.log(`gen:api wrote ${committed}`);
} else {
  const dir = mkdtempSync(join(tmpdir(), 'tn-api-'));
  const fresh = join(dir, 'schema.d.ts');
  try {
    generate(fresh);
    const a = readFileSync(committed, 'utf8');
    const b = readFileSync(fresh, 'utf8');
    if (a !== b) {
      const al = a.split('\n');
      const bl = b.split('\n');
      let i = 0;
      while (i < Math.min(al.length, bl.length) && al[i] === bl[i]) i++;
      console.error(`check:api FAILED: ${committed} is stale against ${contract}.`);
      console.error(`First difference at line ${i + 1}:`);
      console.error(`  committed: ${al[i] ?? '<EOF>'}`);
      console.error(`  generated: ${bl[i] ?? '<EOF>'}`);
      console.error('Run `npm run gen:api` and commit the result.');
      process.exitCode = 1;
    } else {
      console.log('check:api OK: schema.d.ts matches docs/interfaces/openapi.json');
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
