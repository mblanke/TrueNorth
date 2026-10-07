import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { CdkDrag, CdkDragDrop, CdkDropList, CdkDropListGroup, moveItemInArray, transferArrayItem } from '@angular/cdk/drag-drop';
import { MatButtonModule } from '@angular/material/button';
import { catchError, of } from 'rxjs';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { BoardColumn, STATUS_LABELS, SupportQueue, TicketSummary, TicketsApiService } from '@core/services/tickets-api.service';

/** Kanban: one column per status; dragging a card changes its status and position. */
@Component({
  selector: 'tn-ticket-board',
  standalone: true,
  imports: [FormsModule, RouterLink, CdkDropListGroup, CdkDropList, CdkDrag, MatButtonModule, KbStylesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb" style="max-width:none">
      <div class="tn-kb-crumbs"><a routerLink="/support">Support</a> › Board</div>
      <div class="tn-kb-head">
        <div>
          <h1>Support board</h1>
          <p class="tn-kb-muted">Drag cards between columns.</p>
        </div>
        <div class="tn-kb-actions">
          <select class="tn-kb-input" style="width:170px" aria-label="Queue" [(ngModel)]="queueId" (ngModelChange)="load()">
            <option value="">All queues</option>
            @for (q of queues(); track q.id) { <option [value]="q.id">{{ q.name }}</option> }
          </select>
          <select class="tn-kb-input" style="width:140px" aria-label="Assignee" [(ngModel)]="assignee" (ngModelChange)="load()">
            <option value="">Anyone</option>
            <option value="me">Me</option>
          </select>
          <a mat-flat-button color="primary" routerLink="/support/new">New ticket</a>
        </div>
      </div>

      <div class="tn-kb-board" cdkDropListGroup>
        @for (col of columns(); track col.status) {
          <div class="tn-kb-col" cdkDropList [cdkDropListData]="col" (cdkDropListDropped)="drop($event)"
               [attr.aria-label]="labels[col.status]">
            <h2>{{ labels[col.status] }} · {{ col.tickets.length }}</h2>
            @for (t of col.tickets; track t.id) {
              <a class="tn-kb-card" cdkDrag [cdkDragData]="t" [routerLink]="['/support', t.id]">
                <div>{{ t.subject }}</div>
                <div class="meta">
                  <span class="tn-kb-key">{{ t.key }}</span>
                  <span>
                    @if (t.priority === 'high' || t.priority === 'critical') {
                      <span class="tn-kb-tag" [class]="t.priority">{{ t.priority }}</span>
                    }
                    {{ t.assignee_name ? lastName(t.assignee_name) : 'unassigned' }}
                  </span>
                </div>
              </a>
            }
          </div>
        }
      </div>
    </div>
  `,
})
export class TicketBoardComponent implements OnInit {
  private readonly api = inject(TicketsApiService);
  private readonly notify = inject(NotificationService);

  readonly labels = STATUS_LABELS;
  readonly columns = signal<BoardColumn[]>([]);
  readonly queues = signal<SupportQueue[]>([]);
  queueId = '';
  assignee = '';

  ngOnInit(): void {
    this.api.queues().pipe(catchError(() => of([]))).subscribe(q => this.queues.set(q));
    this.load();
  }

  load(): void {
    this.api.board({ queue_id: this.queueId || undefined, assignee: this.assignee || undefined }).subscribe({
      next: c => this.columns.set(c),
      error: () => this.notify.error('Could not load the board'),
    });
  }

  lastName(name: string): string {
    return name.trim().split(/\s+/).pop() ?? name;
  }

  drop(ev: CdkDragDrop<BoardColumn>): void {
    const from = ev.previousContainer.data.tickets;
    const to = ev.container.data.tickets;
    if (ev.previousContainer === ev.container) {
      if (ev.previousIndex === ev.currentIndex) return;
      moveItemInArray(to, ev.previousIndex, ev.currentIndex);
    } else {
      transferArrayItem(from, to, ev.previousIndex, ev.currentIndex);
    }
    const card: TicketSummary = to[ev.currentIndex];
    const status = ev.container.data.status;
    const order = this.orderBetween(to[ev.currentIndex - 1]?.board_order, to[ev.currentIndex + 1]?.board_order);
    card.status = status;
    card.board_order = order;
    this.columns.set([...this.columns()]);
    this.api.move(card.id, status, order).subscribe({
      error: () => { this.notify.error(`Could not move ${card.key}`); this.load(); },
    });
  }

  /** A position between the neighbours, so only the moved card is written. */
  private orderBetween(before?: number, after?: number): number {
    if (before === undefined && after === undefined) return 0;
    if (before === undefined) return (after as number) - 1;
    if (after === undefined) return before + 1;
    return before === after ? before + 0.001 : (before + after) / 2;
  }
}
