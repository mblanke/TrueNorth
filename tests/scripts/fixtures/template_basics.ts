/* Fixture for tests/scripts/test_count_ui_literals.py — class A/B scanner
   policies on a miniature inline template. Not compiled by Angular; the real
   components live under control-plane/web/src/app. Each line pins one policy
   decision; the expected counts live in TestCommittedFixtures. */

import { Component } from '@angular/core';

@Component({
  template: `
    <h1>Sign in to the range</h1>
    <!-- hidden <p>commented words</p> -->
    <mat-icon>school</mat-icon>
    <style>.x { color: red; }</style>
    @if (mode === 'individual') { A single range }
    @else { One shared range }
    @case 'x' { Bare case label }
    <p>Hello {{ name }} world</p>
    <p>{{ warn ? 'Required field' : '' }}</p>
    <p>{{ started | date:'mediumDate' }}</p>
    <p>Q&amp;A</p>
    <p>42 · 100%</p>
    <input placeholder="e.g. medical appointment" title="Copy password">
    <input [placeholder]="warn ? 'Required' : ''" [attr.aria-label]="lbl">
    <div class="hero" (click)="login()" [routerLink]="['/dashboard']"></div>
  `,
})
export class FixtureTemplateBasicsComponent {}
