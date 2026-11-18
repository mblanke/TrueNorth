/* Fixture for tests/scripts/test_count_ui_literals.py — class C policies on
   miniature TypeScript. Not compiled by Angular. Each line pins one policy
   decision; the expected count lives in TestCommittedFixtures. */

import { Injectable } from '@angular/core';

const url = '/dashboard'; // router path — not UI
import gsap from 'gsap'; // import specifier — not UI
const ease = 'power3.out'; // gsap ease — not UI
const query = '(min-width: 1024px)'; // media query — not UI
const width = 'calc(100% - 64px)'; // css calc — not UI
// a 'quoted' remark — comments never count
/* block 'quote' — also never counts */
const label = STATUS_LABEL[status]; // key lookup — not a literal

const msg = 'Your range could not be built.'; // prose — counts
const saved = `Class ${c.name} ${f.id ? 'saved' : 'created'}`; // 'Class', 'saved', 'created'
const seat = seats.find((s) => s.status === 'forfeited'); // KEEP_WORD
const units = `${min} min`; // KEEP_WORD inside a template run
const rowLabel = 'RAM'; // KEEP_WORD on its own

@Component({ template: `<p>Template words</p>`, styles: [`p { color: red; }`] })
export class FixtureTsLiteralsService {}
