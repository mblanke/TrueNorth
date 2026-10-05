import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { SupportQueue, TicketsApiService } from '@core/services/tickets-api.service';

/** Admin: the triage queues tickets are filed into. One is the default for new tickets. */
@Component({
  selector: 'tn-ticket-queues',
  standalone: true,
  imports: [FormsModule, RouterLink, MatButtonModule, KbStylesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb" style="max-width:820px">
      <div class="tn-kb-crumbs"><a routerLink="/support">Support</a> › Queues</div>
      <div class="tn-kb-head">
        <div>
          <h1>Queues</h1>
          <p class="tn-kb-muted">Where tickets land. Students' tickets go to the default queue.</p>
        </div>
      </div>
      <section class="tn-kb-panel">
        @for (q of queues(); track q.id) {
          <div class="tn-kb-row">
            <div>
              <strong>{{ q.name }}</strong>
              @if (q.is_default) { <span class="tn-kb-tag" style="margin-left:6px">default</span> }
              @if (q.description) { <p class="tn-kb-small tn-kb-muted">{{ q.description }}</p> }
            </div>
            <div class="tn-kb-actions">
              @if (!q.is_default) { <button mat-button type="button" (click)="makeDefault(q)">Make default</button> }
              <button mat-button color="warn" type="button" (click)="remove(q)">Delete</button>
            </div>
          </div>
        }
      </section>
      <form class="tn-kb-panel" (ngSubmit)="create()">
        <h2>New queue</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <label class="tn-kb-field" for="q-name" style="margin-top:0">Name</label>
            <input id="q-name" class="tn-kb-input" name="name" required [(ngModel)]="name">
          </div>
          <div>
            <label class="tn-kb-field" for="q-desc" style="margin-top:0">Description</label>
            <input id="q-desc" class="tn-kb-input" name="description" [(ngModel)]="description">
          </div>
        </div>
        <div class="tn-kb-actions" style="margin-top:12px">
          <button mat-flat-button color="primary" type="submit" [disabled]="!name.trim()">Add queue</button>
        </div>
      </form>
    </div>
  `,
})
export class TicketQueuesComponent implements OnInit {
  private readonly api = inject(TicketsApiService);
  private readonly notify = inject(NotificationService);

  readonly queues = signal<SupportQueue[]>([]);
  name = '';
  description = '';

  ngOnInit(): void {
    this.load();
  }

  create(): void {
    const slug = this.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 100) || 'queue';
    this.api.createQueue({ name: this.name.trim(), slug, description: this.description, is_default: false }).subscribe({
      next: () => { this.name = ''; this.description = ''; this.load(); },
      error: err => this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not add the queue'),
    });
  }

  makeDefault(q: SupportQueue): void {
    this.api.updateQueue(q.id, { is_default: true }).subscribe({ next: () => this.load() });
  }

  remove(q: SupportQueue): void {
    if (!confirm(`Delete the ${q.name} queue?`)) return;
    this.api.deleteQueue(q.id).subscribe({
      next: () => this.load(),
      error: err => this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not delete the queue'),
    });
  }

  private load(): void {
    this.api.queues().subscribe(q => this.queues.set(q));
  }
}
