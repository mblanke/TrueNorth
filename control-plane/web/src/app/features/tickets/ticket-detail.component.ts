import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { catchError, forkJoin, of } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { isAdminRole } from '@shared/kb-roles';
import { MarkdownEditorComponent } from '@shared/markdown/markdown-editor.component';
import { MarkdownViewComponent } from '@shared/markdown/markdown-view.component';
import {
  Assignee, PRIORITIES, STATUSES, STATUS_LABELS, SupportQueue, TYPE_LABELS, Ticket, TicketActivity,
  TicketAttachment, TicketComment, TicketUpdate, TicketsApiService,
} from '@core/services/tickets-api.service';

const FIELD_LABELS: Record<string, string> = {
  status: 'status', priority: 'priority', type: 'type', assignee_id: 'assignee', queue_id: 'queue', subject: 'subject',
  range_id: 'range', exercise_id: 'exercise',
};

@Component({
  selector: 'tn-ticket-detail',
  standalone: true,
  imports: [DatePipe, FormsModule, RouterLink, MatButtonModule, KbStylesComponent, MarkdownEditorComponent, MarkdownViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb">
      <div class="tn-kb-crumbs"><a routerLink="/support">Support</a> › {{ ticket()?.key ?? '…' }}</div>
      @if (ticket(); as t) {
        <div class="tn-kb-head">
          <div>
            <h1>{{ t.subject }}</h1>
            <p class="tn-kb-small tn-kb-muted">
              <span class="tn-kb-key">{{ t.key }}</span> · reported by {{ t.reporter_name || 'unknown' }} · {{ t.created_at | date: 'medium' }}
            </p>
          </div>
          @if (!t.can_work && isReporter()) {
            <div class="tn-kb-actions">
              @if (t.status === 'resolved') { <button mat-stroked-button type="button" (click)="patch({ status: 'closed' })">Close ticket</button> }
              @if (t.status === 'resolved' || t.status === 'closed') { <button mat-button type="button" (click)="patch({ status: 'open' })">Reopen</button> }
            </div>
          }
        </div>

        <div class="tn-kb-detail">
          <div style="min-width:0">
            <section class="tn-kb-panel">
              @if (t.description.trim()) { <tn-markdown-view [source]="t.description" /> }
              @else { <p class="tn-kb-muted">No details given.</p> }
              <div class="tn-kb-small" style="margin-top:12px">
                @for (a of attachments(); track a.id) {
                  <div style="display:flex;gap:8px;align-items:center">
                    <a class="tn-kb-link" role="button" tabindex="0" (click)="download(a)" (keydown.enter)="download(a)">{{ a.filename }}</a>
                    <span class="tn-kb-muted">{{ size(a.size_bytes) }}</span>
                    <a class="tn-kb-link tn-kb-muted" role="button" tabindex="0" aria-label="Remove attachment"
                       (click)="removeAttachment(a)" (keydown.enter)="removeAttachment(a)">remove</a>
                  </div>
                }
                <label class="tn-kb-link" style="cursor:pointer;display:inline-block;margin-top:6px">
                  + Attach files <input type="file" multiple hidden (change)="attach($event)">
                </label>
              </div>
            </section>

            <section class="tn-kb-panel">
              <h2>Conversation</h2>
              @for (c of comments(); track c.id) {
                <div class="tn-kb-comment" [class.internal]="c.is_internal">
                  <strong>{{ c.author_name || 'Someone' }}</strong>
                  @if (c.is_internal) { <span class="tn-kb-tag" style="margin-left:6px">Internal note</span> }
                  <span class="tn-kb-small tn-kb-muted"> · {{ c.created_at | date: 'short' }}</span>
                  <div style="margin-top:4px"><tn-markdown-view [source]="c.body" /></div>
                </div>
              } @empty {
                <p class="tn-kb-muted tn-kb-small">No replies yet.</p>
              }
              <span class="tn-kb-field">Reply</span>
              <tn-markdown-editor label="Reply" [minHeight]="90" [showPreview]="false" placeholder="Write a reply. Markdown works."
                                  [(value)]="reply" />
              <div class="tn-kb-actions" style="margin-top:10px">
                <button mat-flat-button color="primary" type="button" [disabled]="sending() || !reply.trim()" (click)="send()">Send</button>
                @if (t.can_work) {
                  <label class="tn-kb-small" style="display:flex;gap:6px;align-items:center">
                    <input type="checkbox" [(ngModel)]="internal"> Internal note (staff only)
                  </label>
                }
              </div>
            </section>

            <section class="tn-kb-panel tn-kb-small">
              <h2>History</h2>
              @for (a of activity(); track a.id) {
                <div class="tn-kb-muted">{{ describe(a) }} · {{ a.created_at | date: 'short' }}</div>
              }
            </section>
          </div>

          <aside class="tn-kb-panel">
            <dl class="tn-kb-kv">
              <dt>Status</dt>
              <dd>
                @if (t.can_work) {
                  <select class="tn-kb-input" aria-label="Status" [ngModel]="t.status" (ngModelChange)="patch({ status: $event })">
                    @for (s of statuses; track s) { <option [value]="s">{{ statusLabels[s] }}</option> }
                  </select>
                } @else { {{ statusLabels[t.status] }} }
              </dd>
              <dt>Priority</dt>
              <dd>
                @if (t.can_work) {
                  <select class="tn-kb-input" aria-label="Priority" [ngModel]="t.priority" (ngModelChange)="patch({ priority: $event })">
                    @for (p of priorities; track p) { <option [value]="p">{{ p }}</option> }
                  </select>
                } @else { <span class="tn-kb-tag" [class]="t.priority">{{ t.priority }}</span> }
              </dd>
              <dt>Assignee</dt>
              <dd>
                @if (t.can_work) {
                  <select class="tn-kb-input" aria-label="Assignee" [ngModel]="t.assignee_id ?? ''" (ngModelChange)="assign($event)">
                    <option value="">Unassigned</option>
                    @for (a of assignees(); track a.id) { <option [value]="a.id">{{ a.display_name }}</option> }
                  </select>
                } @else { {{ t.assignee_name || 'Not yet assigned' }} }
              </dd>
              <dt>Type</dt>
              <dd>
                @if (t.can_work) {
                  <select class="tn-kb-input" aria-label="Type" [ngModel]="t.type" (ngModelChange)="patch({ type: $event })">
                    @for (k of typeKeys; track k) { <option [value]="k">{{ typeLabels[k] }}</option> }
                  </select>
                } @else { {{ typeLabels[t.type] }} }
              </dd>
              @if (t.can_work) {
                <dt>Queue</dt>
                <dd>
                  <select class="tn-kb-input" aria-label="Queue" [ngModel]="t.queue_id" (ngModelChange)="patch({ queue_id: $event })">
                    @for (q of queues(); track q.id) { <option [value]="q.id">{{ q.name }}</option> }
                  </select>
                </dd>
              }
              @if (t.range_name) {
                <dt>Range</dt>
                <dd>{{ t.range_name }}
                  @if (t.can_work) {
                    <a class="tn-kb-link tn-kb-small" role="button" tabindex="0" aria-label="Remove the range link"
                       (click)="patch({ unlink_range: true })" (keydown.enter)="patch({ unlink_range: true })">remove</a>
                  }
                </dd>
              }
              @if (t.exercise_name) {
                <dt>Exercise</dt>
                <dd>{{ t.exercise_name }}
                  @if (t.can_work) {
                    <a class="tn-kb-link tn-kb-small" role="button" tabindex="0" aria-label="Remove the exercise link"
                       (click)="patch({ unlink_exercise: true })" (keydown.enter)="patch({ unlink_exercise: true })">remove</a>
                  }
                </dd>
              }
              @if (t.resolved_at) { <dt>Resolved</dt><dd class="tn-kb-small">{{ t.resolved_at | date: 'short' }}</dd> }
            </dl>
            @if (admin()) {
              <p style="margin:16px 0 0"><button mat-button color="warn" type="button" (click)="remove(t)">Delete ticket</button></p>
            }
          </aside>
        </div>
      } @else if (missing()) {
        <div class="tn-kb-panel"><p>This ticket doesn't exist, or you don't have access to it.</p></div>
      } @else {
        <p class="tn-kb-muted">Loading…</p>
      }
    </div>
  `,
})
export class TicketDetailComponent implements OnInit {
  private readonly api = inject(TicketsApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly statuses = STATUSES;
  readonly statusLabels = STATUS_LABELS;
  readonly priorities = PRIORITIES;
  readonly typeLabels = TYPE_LABELS;
  readonly typeKeys = Object.keys(TYPE_LABELS) as (keyof typeof TYPE_LABELS)[];

  readonly ticket = signal<Ticket | null>(null);
  readonly comments = signal<TicketComment[]>([]);
  readonly attachments = signal<TicketAttachment[]>([]);
  readonly activity = signal<TicketActivity[]>([]);
  readonly assignees = signal<Assignee[]>([]);
  readonly queues = signal<SupportQueue[]>([]);
  readonly missing = signal(false);
  readonly sending = signal(false);
  readonly admin = computed(() => isAdminRole(this.auth.user()?.role));
  readonly isReporter = computed(() => this.ticket()?.reporter_id === this.auth.user()?.id);

  reply = '';
  internal = false;
  private id = '';

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
    this.api.get(this.id).subscribe({
      next: t => {
        this.ticket.set(t);
        this.refreshSide();
        if (t.can_work) {
          this.api.assignees().pipe(catchError(() => of([]))).subscribe(a => this.assignees.set(a));
          this.api.queues().pipe(catchError(() => of([]))).subscribe(q => this.queues.set(q));
        }
      },
      error: () => this.missing.set(true),
    });
  }

  patch(body: TicketUpdate): void {
    this.api.update(this.id, body).subscribe({
      next: t => { this.ticket.set(t); this.loadActivity(); },
      error: err => {
        this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not update the ticket');
        this.ticket.update(t => (t ? { ...t } : t)); // re-render selects back to the saved value
      },
    });
  }

  assign(userId: string): void {
    this.patch(userId ? { assignee_id: userId } : { unassign: true });
  }

  send(): void {
    this.sending.set(true);
    this.api.addComment(this.id, this.reply.trim(), this.internal).subscribe({
      next: () => {
        this.reply = '';
        this.internal = false;
        this.sending.set(false);
        this.refreshSide();
        this.api.get(this.id).subscribe(t => this.ticket.set(t)); // a reply can reopen a waiting ticket
      },
      error: () => { this.sending.set(false); this.notify.error('Could not send the reply'); },
    });
  }

  attach(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (!files.length) return;
    this.api.upload(this.id, files).subscribe({
      next: () => this.refreshSide(),
      error: err => this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Upload failed'),
    });
  }

  download(a: TicketAttachment): void {
    this.api.download(a.id).subscribe({
      next: blob => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = a.filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      },
      error: () => this.notify.error('Could not download the file'),
    });
  }

  removeAttachment(a: TicketAttachment): void {
    if (!confirm(`Remove ${a.filename}?`)) return;
    this.api.removeAttachment(a.id).subscribe({
      next: () => this.refreshSide(),
      error: err => this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not remove it'),
    });
  }

  remove(t: Ticket): void {
    if (!confirm(`Delete ${t.key}? It disappears from every list.`)) return;
    this.api.remove(t.id).subscribe({
      next: () => { this.notify.success(`${t.key} deleted`); this.router.navigate(['/support']); },
      error: () => this.notify.error('Could not delete the ticket'),
    });
  }

  size(bytes: number): string {
    return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }

  describe(a: TicketActivity): string {
    const who = a.actor_name || 'Someone';
    if (a.field === 'created') return `${who} created ${a.new_value}`;
    if (a.field === 'attachment') return a.new_value ? `${who} attached ${a.new_value}` : `${who} removed ${a.old_value}`;
    const label = FIELD_LABELS[a.field] ?? a.field;
    // The API already turns assignee / queue ids into names.
    const show = (v: string) => (!v ? 'none' : a.field === 'status' ? STATUS_LABELS[v as keyof typeof STATUS_LABELS] ?? v : v);
    return `${who} changed ${label} from ${show(a.old_value)} to ${show(a.new_value)}`;
  }

  private refreshSide(): void {
    forkJoin({
      comments: this.api.comments(this.id).pipe(catchError(() => of([]))),
      attachments: this.api.attachments(this.id).pipe(catchError(() => of([]))),
    }).subscribe(r => {
      this.comments.set(r.comments);
      this.attachments.set(r.attachments);
    });
    this.loadActivity();
  }

  private loadActivity(): void {
    this.api.activity(this.id).pipe(catchError(() => of([]))).subscribe(a => this.activity.set(a));
  }
}
