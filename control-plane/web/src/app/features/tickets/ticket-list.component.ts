import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { AuthService } from '@core/services/auth.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { canFileTickets, isAdminRole, isStaffRole } from '@shared/kb-roles';
import { STATUS_LABELS, TicketFilters, TicketSummary, TicketsApiService } from '@core/services/tickets-api.service';

interface Tab { key: string; label: string; filters: TicketFilters }

const ACTIVE = 'open,in_progress,waiting';
const DONE = 'resolved,closed';

const STAFF_TABS: Tab[] = [
  { key: 'assigned', label: 'Assigned to me', filters: { scope: 'assigned', status: ACTIVE } },
  { key: 'open', label: 'All open', filters: { scope: 'all', status: ACTIVE } },
  { key: 'mine', label: 'Reported by me', filters: { scope: 'mine' } },
  { key: 'done', label: 'Resolved & closed', filters: { scope: 'all', status: DONE } },
];
const STUDENT_TABS: Tab[] = [
  { key: 'open', label: 'Open', filters: { scope: 'mine', status: ACTIVE } },
  { key: 'done', label: 'Resolved & closed', filters: { scope: 'mine', status: DONE } },
];

@Component({
  selector: 'tn-ticket-list',
  standalone: true,
  imports: [DatePipe, FormsModule, RouterLink, MatButtonModule, KbStylesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb">
      <div class="tn-kb-head">
        <div>
          <h1>Support</h1>
          <p class="tn-kb-muted">{{ staff() ? 'Every ticket in your organisation.' : 'Problems you have reported.' }}</p>
        </div>
        <div class="tn-kb-actions">
          @if (staff()) { <a mat-button routerLink="/support/board">Board</a> }
          @if (admin()) { <a mat-button routerLink="/support/queues">Queues</a> }
          @if (canFile()) { <a mat-flat-button color="primary" routerLink="/support/new">Report a problem</a> }
        </div>
      </div>

      <div class="tn-kb-tabs" role="group" aria-label="Which tickets">
        @for (t of tabs(); track t.key) {
          <button type="button" [attr.aria-pressed]="tab() === t.key" (click)="select(t.key)">{{ t.label }}</button>
        }
        <input class="tn-kb-input" style="width:220px;margin:0 0 6px auto" type="search" placeholder="Search or TN-number"
               aria-label="Search tickets" [(ngModel)]="q" (keyup.enter)="load()" (search)="load()">
      </div>

      <section class="tn-kb-panel">
        @if (tickets().length) {
          <div class="tn-kb-scroll">
          <table class="tn-kb-table">
            <thead>
              <tr>
                <th style="width:72px">Key</th><th>Subject</th>
                @if (staff()) { <th style="width:140px">Reporter</th><th style="width:140px">Assignee</th> }
                <th style="width:150px">Status</th><th style="width:84px">Priority</th><th style="width:130px">Updated</th>
              </tr>
            </thead>
            <tbody>
              @for (t of tickets(); track t.id) {
                <tr>
                  <td class="tn-kb-key">{{ t.key }}</td>
                  <td>
                    <a class="tn-kb-link" [routerLink]="['/support', t.id]">{{ t.subject }}</a>
                    @if (t.queue_name && staff()) { <div class="tn-kb-small tn-kb-muted">{{ t.queue_name }}</div> }
                  </td>
                  @if (staff()) {
                    <td>{{ t.reporter_name || '—' }}</td>
                    <td>{{ t.assignee_name || '—' }}</td>
                  }
                  <td>{{ labels[t.status] }}</td>
                  <td><span class="tn-kb-tag" [class]="t.priority">{{ t.priority }}</span></td>
                  <td class="tn-kb-small tn-kb-muted">{{ t.updated_at | date: 'short' }}</td>
                </tr>
              }
            </tbody>
          </table>
          </div>
        } @else if (forbidden()) {
          <p class="tn-kb-muted">Your role can read the wiki but can't file or view support tickets. Ask an instructor for help.</p>
        } @else {
          <p class="tn-kb-muted">{{ loading() ? 'Loading…' : 'No tickets here.' }}</p>
        }
      </section>
    </div>
  `,
})
export class TicketListComponent implements OnInit {
  private readonly api = inject(TicketsApiService);
  private readonly auth = inject(AuthService);

  readonly labels = STATUS_LABELS;
  readonly staff = computed(() => isStaffRole(this.auth.user()?.role));
  readonly admin = computed(() => isAdminRole(this.auth.user()?.role));
  readonly canFile = computed(() => canFileTickets(this.auth.user()?.role));
  readonly tabs = computed(() => (this.staff() ? STAFF_TABS : STUDENT_TABS));
  readonly tab = signal('');
  readonly tickets = signal<TicketSummary[]>([]);
  readonly loading = signal(true);
  readonly forbidden = signal(false);
  q = '';

  ngOnInit(): void {
    let saved = '';
    try { saved = sessionStorage.getItem('tn-support-tab') ?? ''; } catch { /* storage may be blocked */ }
    this.tab.set(this.tabs().some(t => t.key === saved) ? saved : this.tabs()[this.staff() ? 1 : 0].key);
    this.load();
  }

  select(key: string): void {
    this.tab.set(key);
    try { sessionStorage.setItem('tn-support-tab', key); } catch { /* storage may be blocked */ }
    this.load();
  }

  load(): void {
    const tab = this.tabs().find(t => t.key === this.tab()) ?? this.tabs()[0];
    this.loading.set(true);
    this.api.list({ ...tab.filters, q: this.q.trim() || undefined }).subscribe({
      next: t => { this.tickets.set(t); this.loading.set(false); },
      error: err => { this.tickets.set([]); this.loading.set(false); this.forbidden.set(err?.status === 403); },
    });
  }
}
