import { Component, OnInit, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { ClassesApiService, RollCallDomains } from '@core/services/classes-api.service';
import { apiError } from '../ranges/range-capabilities';

/** Domains typed one per line, or separated by commas or spaces; "@" and case dropped. */
export function parseDomains(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const d = raw.trim().toLowerCase().replace(/^@+/, '');
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
}

/** What the current setting means, in one sentence. */
export function domainsSummary(d: RollCallDomains): string {
  if (d.source === 'setting') {
    return d.email_domains.length
      ? `Roll calls accept addresses at ${d.email_domains.join(', ')}.`
      : 'Roll calls accept any domain, in smaller batches (50 at a time).';
  }
  return d.email_domains.length
    ? `Not set by an admin: roll calls accept the domains of your organisation's AD accounts (${d.email_domains.join(', ')}).`
    : "Not set, and your organisation has no AD accounts yet: every roll-call add is refused until an admin sets them.";
}

/**
 * Classes → Roll-call domains (#318): the email domains a roll call may add for this
 * organisation (GET/PUT /classes/roll-call-domains). The API's refusal when none are set
 * points here. Staff see the setting; an admin (tenant:update) changes it. Audited
 * (roll_call_domains_set).
 */
@Component({
  selector: 'tn-roll-call-domains-card',
  imports: [FormsModule, MatButtonModule, MatCardModule, MatCheckboxModule, MatFormFieldModule, MatIconModule, MatInputModule],
  template: `
    @if (domains(); as d) {
      <mat-card class="domains-card">
        <mat-card-header>
          <mat-icon mat-card-avatar>alternate_email</mat-icon>
          <mat-card-title>Roll-call domains</mat-card-title>
          <mat-card-subtitle>{{ summary() }}</mat-card-subtitle>
        </mat-card-header>
        @if (canEdit()) {
          <mat-card-content>
            @if (!editing()) {
              <button mat-stroked-button (click)="startEdit(d)"><mat-icon>edit</mat-icon> Change</button>
            } @else {
              <mat-checkbox [ngModel]="anyDomain()" (ngModelChange)="anyDomain.set($event)">
                Any domain (roll calls then add at most 50 at a time)
              </mat-checkbox>
              @if (!anyDomain()) {
                <mat-form-field appearance="outline" class="full">
                  <mat-label>Allowed email domains</mat-label>
                  <textarea matInput rows="3" [ngModel]="text()" (ngModelChange)="text.set($event)"
                            placeholder="forces.gc.ca"></textarea>
                  <mat-hint>One per line, or separated by commas.</mat-hint>
                </mat-form-field>
              }
              @if (error()) { <p class="error" role="alert">{{ error() }}</p> }
              <div class="actions">
                <button mat-flat-button color="primary" (click)="save()" [disabled]="saving()">Save</button>
                <button mat-button (click)="editing.set(false)" [disabled]="saving()">Cancel</button>
              </div>
            }
          </mat-card-content>
        }
      </mat-card>
    }
  `,
  styles: [`
    .domains-card { margin-bottom: 16px; }
    .full { width: 100%; margin-top: 8px; }
    .actions { display: flex; gap: 8px; margin-top: 8px; }
    .error { color: var(--mat-sys-error); }
  `],
})
export class RollCallDomainsCardComponent implements OnInit {
  private readonly api = inject(ClassesApiService);

  /** An admin (tenant:update) may change it; other staff only read it. */
  readonly canEdit = input(false);

  readonly domains = signal<RollCallDomains | null>(null);
  readonly summary = signal('');
  readonly editing = signal(false);
  readonly anyDomain = signal(false);
  readonly text = signal('');
  readonly saving = signal(false);
  readonly error = signal('');

  ngOnInit(): void {
    this.api.rollCallDomains().subscribe({ next: d => this.show(d), error: () => {} });
  }

  startEdit(d: RollCallDomains): void {
    this.anyDomain.set(d.source === 'setting' && d.email_domains.length === 0);
    this.text.set(d.email_domains.join('\n'));
    this.error.set('');
    this.editing.set(true);
  }

  save(): void {
    const list = this.anyDomain() ? [] : parseDomains(this.text());
    if (!this.anyDomain() && list.length === 0) {
      this.error.set('Enter at least one domain, or tick "Any domain".');
      return;
    }
    this.saving.set(true);
    this.error.set('');
    this.api.setRollCallDomains(list).subscribe({
      next: d => { this.saving.set(false); this.editing.set(false); this.show(d); },
      error: err => { this.saving.set(false); this.error.set(apiError(err)); },
    });
  }

  private show(d: RollCallDomains): void {
    this.domains.set(d);
    this.summary.set(domainsSummary(d));
  }
}
