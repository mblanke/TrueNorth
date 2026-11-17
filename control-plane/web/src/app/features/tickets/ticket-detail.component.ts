import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { catchError, forkJoin, map, of, switchMap } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { apiErrorMessage } from '@shared/kb-errors';
import { isAdminRole } from '@shared/kb-roles';
import { MarkdownEditorComponent } from '@shared/markdown/markdown-editor.component';
import { MarkdownViewComponent } from '@shared/markdown/markdown-view.component';
import {
  Assignee, PRIORITIES, STATUSES, STATUS_LABELS, SupportQueue, TYPE_LABELS, Ticket, TicketActivity,
  TicketAttachment, TicketCategory, TicketComment, TicketLabelCount, TicketTriage, TicketUpdate, TicketsApiService, labelList,
} from '@core/services/tickets-api.service';

const FIELD_LABELS: Record<string, string> = {
  status: 'status', priority: 'priority', type: 'type', assignee_id: 'assignee', queue_id: 'queue', subject: 'subject',
  range_id: 'range', exercise_id: 'exercise', category: 'category', labels: 'labels',
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
            @if (editing()) {
              <label class="tn-kb-field" for="tk-edit-subject" style="margin-top:0">Subject</label>
              <input id="tk-edit-subject" class="tn-kb-input" maxlength="500" [(ngModel)]="editSubject">
            } @else {
              <h1>{{ t.subject }}</h1>
            }
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
              @if (editing()) {
                <tn-markdown-editor label="Details" [minHeight]="150" [showPreview]="false" [(value)]="editDescription" />
                <div class="tn-kb-actions" style="margin-top:10px">
                  <button mat-flat-button color="primary" type="button" [disabled]="!editSubject.trim()" (click)="saveEdit()">Save</button>
                  <button mat-button type="button" (click)="editing.set(false)">Cancel</button>
                </div>
              } @else {
                @if (t.description.trim()) { <tn-markdown-view [source]="t.description" /> }
                @else { <p class="tn-kb-muted">No details given.</p> }
                @if (canEdit()) {
                  <p style="margin:8px 0 0"><button type="button" class="tn-kb-linkbtn tn-kb-small" (click)="startEdit(t)">Edit subject and details</button></p>
                }
              }
              <div class="tn-kb-small" style="margin-top:12px">
                @for (a of attachments(); track a.id) {
                  <div style="display:flex;gap:8px;align-items:center">
                    <button type="button" class="tn-kb-linkbtn" (click)="download(a)">{{ a.filename }}</button>
                    <span class="tn-kb-muted">{{ size(a.size_bytes) }}</span>
                    @if (canRemove(a)) {
                      <button type="button" class="tn-kb-linkbtn muted" [attr.aria-label]="'Remove ' + a.filename"
                              (click)="removeAttachment(a)">remove</button>
                    }
                  </div>
                }
                <button type="button" class="tn-kb-linkbtn" style="margin-top:6px" (click)="filePicker.click()">+ Attach files</button>
                <input #filePicker type="file" multiple class="tn-kb-sr-only" tabindex="-1" aria-hidden="true" (change)="attach($event)">
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
                  <select #st class="tn-kb-input" aria-label="Status" (change)="patchField('status', st)">
                    @for (s of statuses; track s) { <option [value]="s" [selected]="s === t.status">{{ statusLabels[s] }}</option> }
                  </select>
                } @else { {{ statusLabels[t.status] }} }
              </dd>
              <dt>Priority</dt>
              <dd>
                @if (t.can_work) {
                  <select #pr class="tn-kb-input" aria-label="Priority" (change)="patchField('priority', pr)">
                    @for (p of priorities; track p) { <option [value]="p" [selected]="p === t.priority">{{ p }}</option> }
                  </select>
                } @else { <span class="tn-kb-tag" [class]="t.priority">{{ t.priority }}</span> }
              </dd>
              <dt>Assignee</dt>
              <dd>
                @if (t.can_work) {
                  <select #asg class="tn-kb-input" aria-label="Assignee" (change)="patchField('assignee_id', asg)">
                    <option value="" [selected]="!t.assignee_id">Unassigned</option>
                    @for (a of assignees(); track a.id) { <option [value]="a.id" [selected]="a.id === t.assignee_id">{{ a.display_name }}</option> }
                  </select>
                } @else { {{ t.assignee_name || 'Not yet assigned' }} }
              </dd>
              <dt>Type</dt>
              <dd>
                @if (t.can_work) {
                  <select #ty class="tn-kb-input" aria-label="Type" (change)="patchField('type', ty)">
                    @for (k of typeKeys; track k) { <option [value]="k" [selected]="k === t.type">{{ typeLabels[k] }}</option> }
                  </select>
                } @else { {{ typeLabels[t.type] }} }
              </dd>
              <dt>About</dt>
              <dd>
                @if (t.can_work) {
                  <select #cat class="tn-kb-input" aria-label="Category" (change)="patchField('category', cat)">
                    @for (c of categoryOptions(t.category); track c.key) {
                      <option [value]="c.key" [selected]="c.key === t.category">{{ c.label }}</option>
                    }
                  </select>
                } @else { {{ categoryLabel(t.category) }} }
              </dd>
              @if (t.can_work) {
                <dt>Labels</dt>
                <dd>
                  <div class="tn-kb-chips">
                    @for (l of labelList(t.labels); track l) {
                      <span class="tn-kb-chip">{{ l }}<button type="button" [attr.aria-label]="'Remove label ' + l"
                                                              (click)="removeLabel(t, l)">×</button></span>
                    }
                  </div>
                  <input #lab class="tn-kb-input" aria-label="Add a label" placeholder="Add a label" list="tk-label-options"
                         autocomplete="off" maxlength="30" (keydown.enter)="addLabel(t, lab); $event.preventDefault()"
                         (change)="addLabel(t, lab)">
                  <datalist id="tk-label-options">
                    @for (l of knownLabels(); track l.label) { <option [value]="l.label"></option> }
                  </datalist>
                </dd>
                <dt>Queue</dt>
                <dd>
                  <select #qu class="tn-kb-input" aria-label="Queue" (change)="patchField('queue_id', qu)">
                    @for (q of queues(); track q.id) { <option [value]="q.id" [selected]="q.id === t.queue_id">{{ q.name }}</option> }
                  </select>
                </dd>
              }
              @if (t.range_name) {
                <dt>Range</dt>
                <dd>{{ t.range_name }}
                  @if (t.can_work) {
                    <button type="button" class="tn-kb-linkbtn tn-kb-small" aria-label="Remove the range link"
                            (click)="patch({ unlink_range: true })">remove</button>
                  }
                </dd>
              }
              @if (t.exercise_name) {
                <dt>Exercise</dt>
                <dd>{{ t.exercise_name }}
                  @if (t.can_work) {
                    <button type="button" class="tn-kb-linkbtn tn-kb-small" aria-label="Remove the exercise link"
                            (click)="patch({ unlink_exercise: true })">remove</button>
                  }
                </dd>
              }
              @if (t.resolved_at) { <dt>Resolved</dt><dd class="tn-kb-small">{{ t.resolved_at | date: 'short' }}</dd> }
            </dl>
            @if (t.can_work && triageAvailable()) {
              <section class="tn-kb-panel" style="margin-top:16px" aria-label="AI triage">
                <strong>AI triage</strong>
                <p class="tn-kb-small tn-kb-muted" style="margin:4px 0 8px">Suggests only; nothing changes until you apply it.
                  The ticket text (no names, no internal notes) is sent to this install's AI backend.</p>
                @if (!suggestion()) {
                  <button mat-stroked-button type="button" [disabled]="triaging()" (click)="suggest()">
                    {{ triaging() ? 'Thinking…' : 'Suggest triage' }}
                  </button>
                  @if (triageError()) { <p class="tn-kb-error" role="alert">{{ triageError() }}</p> }
                } @else {
                  @if (suggestion(); as s) {
                    @if (s.summary) { <p class="tn-kb-small"><strong>Summary.</strong> {{ s.summary }}</p> }
                    @if (s.category && s.category !== t.category) {
                      <p class="tn-kb-small">Category: <strong>{{ categoryLabel(s.category) }}</strong>
                        <button type="button" class="tn-kb-linkbtn" (click)="applyCategory(s.category)">Apply</button></p>
                    }
                    @if (s.priority && s.priority !== t.priority) {
                      <p class="tn-kb-small">Priority: <strong>{{ s.priority }}</strong>
                        <button type="button" class="tn-kb-linkbtn" (click)="applyPriority(s.priority)">Apply</button></p>
                    }
                    @if (newLabels(t, s.labels).length) {
                      <p class="tn-kb-small">Labels: <strong>{{ newLabels(t, s.labels).join(', ') }}</strong>
                        <button type="button" class="tn-kb-linkbtn" (click)="applyLabels(t, s.labels)">Add</button></p>
                    }
                    @if (s.reply_hint) { <p class="tn-kb-small"><strong>Reply hint.</strong> {{ s.reply_hint }}</p> }
                    @if (s.related_pages.length) {
                      <p class="tn-kb-small" style="margin-bottom:2px"><strong>Related pages</strong></p>
                      <ul class="tn-kb-small" style="margin:0;padding-left:18px">
                        @for (p of s.related_pages; track p.page_id) {
                          <li><a [routerLink]="['/wiki', p.space_slug, p.page_id]">{{ p.title }}</a></li>
                        }
                      </ul>
                    }
                    <p class="tn-kb-small tn-kb-muted" style="margin:8px 0 0">By {{ s.model || 'the AI' }}. Check it before you rely on it.
                      <button type="button" class="tn-kb-linkbtn" (click)="suggestion.set(null)">Dismiss</button></p>
                  }
                }
              </section>
            }
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
  private readonly destroyRef = inject(DestroyRef);

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
  readonly categories = signal<TicketCategory[]>([]);
  readonly knownLabels = signal<TicketLabelCount[]>([]);
  readonly labelList = labelList;
  readonly missing = signal(false);
  readonly triageAvailable = signal(false);
  readonly suggestion = signal<TicketTriage | null>(null);
  readonly triaging = signal(false);
  readonly triageError = signal('');
  readonly sending = signal(false);
  readonly admin = computed(() => isAdminRole(this.auth.user()?.role));
  readonly isReporter = computed(() => this.ticket()?.reporter_id === this.auth.user()?.id);
  /** The reporter may edit the subject and details; staff may edit any ticket's. */
  readonly canEdit = computed(() => !!this.ticket() && (this.ticket()!.can_work || this.isReporter()));
  readonly editing = signal(false);

  reply = '';
  internal = false;
  editSubject = '';
  editDescription = '';
  private id = '';
  private staffListsLoaded = false;

  ngOnInit(): void {
    // Follow the route, not a one-time snapshot: a link from one ticket to another
    // reuses this component, and every request must go to the ticket now shown.
    this.route.paramMap
      .pipe(
        map(p => p.get('id') ?? ''),
        switchMap(id => {
          this.reset(id);
          return this.api.get(id).pipe(catchError(() => of(null)));
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(t => {
        if (!t) {
          this.missing.set(true);
          return;
        }
        this.ticket.set(t);
        this.refreshSide();
        if (!this.categories().length) {
          this.api.categories().pipe(catchError(() => of([]))).subscribe(c => this.categories.set(c));
        }
        if (t.can_work && !this.staffListsLoaded) {
          this.staffListsLoaded = true;
          this.api.assignees().pipe(catchError(() => of([]))).subscribe(a => this.assignees.set(a));
          this.api.queues().pipe(catchError(() => of([]))).subscribe(q => this.queues.set(q));
          this.api.labels().pipe(catchError(() => of([]))).subscribe(l => this.knownLabels.set(l));
          this.api.triageAvailable().pipe(catchError(() => of(false))).subscribe(a => this.triageAvailable.set(a));
        }
      });
  }

  suggest(): void {
    const id = this.id;
    this.triaging.set(true);
    this.triageError.set('');
    this.api.triage(id).subscribe({
      next: s => {
        this.triaging.set(false);
        if (id === this.id) this.suggestion.set(s); // the user may have moved to another ticket meanwhile
      },
      error: err => {
        this.triaging.set(false);
        this.triageError.set(apiErrorMessage(err, 'The AI could not suggest anything just now.'));
      },
    });
  }

  applyCategory(category: string): void { this.patch({ category }); }
  applyPriority(priority: string): void { this.patch({ priority } as TicketUpdate); }
  newLabels(t: Ticket, suggested: string[]): string[] {
    const have = labelList(t.labels);
    return suggested.filter(l => !have.includes(l));
  }
  applyLabels(t: Ticket, suggested: string[]): void {
    this.patch({ labels: [...labelList(t.labels), ...this.newLabels(t, suggested)].join(',') });
  }

  canRemove(a: TicketAttachment): boolean {
    return !!this.ticket()?.can_work || a.uploaded_by === this.auth.user()?.id;
  }

  startEdit(t: Ticket): void {
    this.editSubject = t.subject;
    this.editDescription = t.description;
    this.editing.set(true);
  }

  saveEdit(): void {
    this.patch({ subject: this.editSubject.trim(), description: this.editDescription }, () => this.editing.set(false));
  }

  patch(body: TicketUpdate, done?: () => void): void {
    this.api.update(this.id, body).subscribe({
      next: t => { this.ticket.set(t); this.loadActivity(); done?.(); },
      error: err => this.notify.error(apiErrorMessage(err, 'Could not update the ticket')),
    });
  }

  /** A sidebar select changed. If the server refuses, put the select back to the saved value. */
  patchField(field: 'status' | 'priority' | 'type' | 'category' | 'queue_id' | 'assignee_id', el: HTMLSelectElement): void {
    const value = el.value;
    const body: TicketUpdate =
      field === 'assignee_id' ? (value ? { assignee_id: value } : { unassign: true }) : ({ [field]: value } as TicketUpdate);
    this.api.update(this.id, body).subscribe({
      next: t => { this.ticket.set(t); this.loadActivity(); },
      error: err => {
        this.notify.error(apiErrorMessage(err, 'Could not update the ticket'));
        el.value = (this.ticket()?.[field] as string | null) ?? '';
      },
    });
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
      error: err => this.notify.error(apiErrorMessage(err, 'Upload failed')),
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
      error: err => this.notify.error(apiErrorMessage(err, 'Could not remove it')),
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

  /** The categories to offer: the fixed list, plus a legacy value the ticket already holds. */
  categoryOptions(current: string): TicketCategory[] {
    const list = this.categories();
    return list.some(c => c.key === current) ? list : [...list, { key: current, label: current }];
  }

  categoryLabel(key: string): string {
    return this.categories().find(c => c.key === key)?.label ?? key;
  }

  /** Add the typed label(s) (comma separated); the API normalises and validates them. */
  addLabel(t: Ticket, input: HTMLInputElement): void {
    const typed = input.value.trim();
    if (!typed) return;
    input.value = '';
    this.patch({ labels: [...labelList(t.labels), typed].join(',') }, () =>
      this.api.labels().pipe(catchError(() => of([]))).subscribe(l => this.knownLabels.set(l)),
    );
  }

  removeLabel(t: Ticket, label: string): void {
    this.patch({ labels: labelList(t.labels).filter(l => l !== label).join(',') });
  }

  describe(a: TicketActivity): string {
    const who = a.actor_name || 'Someone';
    if (a.field === 'created') return `${who} created ${a.new_value}`;
    if (a.field === 'attachment') return a.new_value ? `${who} attached ${a.new_value}` : `${who} removed ${a.old_value}`;
    if (a.field === 'description') return `${who} edited the details`;
    const label = FIELD_LABELS[a.field] ?? a.field;
    // The API already turns assignee / queue ids into names.
    const show = (v: string) => {
      if (!v) return 'none';
      if (a.field === 'status') return STATUS_LABELS[v as keyof typeof STATUS_LABELS] ?? v;
      if (a.field === 'category') return this.categoryLabel(v);
      if (a.field === 'labels') return v.split(',').join(', ');
      return v;
    };
    return `${who} changed ${label} from ${show(a.old_value)} to ${show(a.new_value)}`;
  }

  private reset(id: string): void {
    this.id = id;
    this.ticket.set(null);
    this.missing.set(false);
    this.comments.set([]);
    this.attachments.set([]);
    this.activity.set([]);
    this.editing.set(false);
    this.suggestion.set(null);
    this.triageError.set('');
    this.reply = '';
    this.internal = false;
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
