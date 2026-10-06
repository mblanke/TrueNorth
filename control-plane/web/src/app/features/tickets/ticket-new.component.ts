import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { catchError, of, switchMap } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { ExerciseSummary, RangeSummary } from '@core/models';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { isStaffRole } from '@shared/kb-roles';
import { MarkdownEditorComponent } from '@shared/markdown/markdown-editor.component';
import { SupportQueue, TYPE_LABELS, TicketPriority, TicketType, TicketsApiService } from '@core/services/tickets-api.service';

/**
 * "Report a problem". Other pages link here with `?range_id=` and/or
 * `?exercise_id=` so the ticket arrives already pointing at the right thing.
 */
@Component({
  selector: 'tn-ticket-new',
  standalone: true,
  imports: [FormsModule, RouterLink, MatButtonModule, KbStylesComponent, MarkdownEditorComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb">
      <div class="tn-kb-crumbs"><a routerLink="/support">Support</a> › New</div>
      <div class="tn-kb-head">
        <div>
          <h1>Report a problem</h1>
          <p class="tn-kb-muted">Range support will pick this up. Replies appear on the ticket.</p>
        </div>
      </div>
      <form class="tn-kb-panel" style="max-width:860px" (ngSubmit)="submit()">
        <label class="tn-kb-field" for="tk-subject" style="margin-top:0">What's wrong?</label>
        <input id="tk-subject" class="tn-kb-input" name="subject" required maxlength="500"
               placeholder="e.g. dc01 won't boot past BIOS" [(ngModel)]="subject">

        <span class="tn-kb-field">Details</span>
        <tn-markdown-editor label="Details" [minHeight]="150" [showPreview]="false"
                            placeholder="What you were doing, what you see, the VM name. Markdown works."
                            [(value)]="description" />

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">
          <div>
            <label class="tn-kb-field" for="tk-type">Type</label>
            <select id="tk-type" class="tn-kb-input" name="type" [(ngModel)]="type">
              @for (t of types(); track t) { <option [value]="t">{{ typeLabels[t] }}</option> }
            </select>
          </div>
          <div>
            <label class="tn-kb-field" for="tk-pri">How urgent?</label>
            <select id="tk-pri" class="tn-kb-input" name="priority" [(ngModel)]="priority">
              <option value="low">Low</option>
              <option value="medium">Normal</option>
              <option value="high">High</option>
              <option value="critical">Blocking my exercise</option>
            </select>
          </div>
          <div>
            <label class="tn-kb-field" for="tk-range">Range</label>
            <select id="tk-range" class="tn-kb-input" name="range" [(ngModel)]="rangeId">
              <option [ngValue]="null">None</option>
              @for (r of ranges(); track r.id) { <option [ngValue]="r.id">{{ r.name }}</option> }
            </select>
          </div>
          <div>
            <label class="tn-kb-field" for="tk-ex">Exercise</label>
            <select id="tk-ex" class="tn-kb-input" name="exercise" [(ngModel)]="exerciseId">
              <option [ngValue]="null">None</option>
              @for (e of exercises(); track e.id) { <option [ngValue]="e.id">{{ e.name }}</option> }
            </select>
          </div>
          @if (staff() && queues().length > 1) {
            <div>
              <label class="tn-kb-field" for="tk-queue">Queue</label>
              <select id="tk-queue" class="tn-kb-input" name="queue" [(ngModel)]="queueId">
                @for (q of queues(); track q.id) { <option [ngValue]="q.id">{{ q.name }}</option> }
              </select>
            </div>
          }
        </div>

        <label class="tn-kb-field" for="tk-files">Screenshots or logs (up to 25 MB each)</label>
        <input id="tk-files" type="file" multiple (change)="pick($event)">

        <div class="tn-kb-actions" style="margin-top:16px">
          <button mat-flat-button color="primary" type="submit" [disabled]="busy() || !subject.trim()">
            {{ busy() ? 'Submitting…' : 'Submit' }}
          </button>
          <a mat-button routerLink="/support">Cancel</a>
        </div>
      </form>
    </div>
  `,
})
export class TicketNewComponent implements OnInit {
  private readonly api = inject(TicketsApiService);
  private readonly core = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly typeLabels = TYPE_LABELS;
  readonly staff = computed(() => isStaffRole(this.auth.user()?.role));
  readonly types = computed<TicketType[]>(() =>
    this.staff() ? ['incident', 'request', 'task', 'bug'] : ['incident', 'request'],
  );
  readonly ranges = signal<RangeSummary[]>([]);
  readonly exercises = signal<ExerciseSummary[]>([]);
  readonly queues = signal<SupportQueue[]>([]);
  readonly busy = signal(false);

  subject = '';
  description = '';
  type: TicketType = 'incident';
  priority: TicketPriority = 'medium';
  rangeId: string | null = null;
  exerciseId: string | null = null;
  queueId: string | null = null;
  private files: File[] = [];

  ngOnInit(): void {
    const qp = this.route.snapshot.queryParamMap;
    this.rangeId = qp.get('range_id');
    this.exerciseId = qp.get('exercise_id');
    // Lists are best-effort: a role that cannot list ranges still files a ticket.
    this.core.listRanges(200).pipe(catchError(() => of([]))).subscribe(r => this.ranges.set(r));
    this.core.listExercises(200).pipe(catchError(() => of([]))).subscribe(e => this.exercises.set(e));
    this.api.queues().pipe(catchError(() => of([]))).subscribe(q => {
      this.queues.set(q);
      this.queueId = q.find(x => x.is_default)?.id ?? q[0]?.id ?? null;
    });
  }

  pick(ev: Event): void {
    this.files = Array.from((ev.target as HTMLInputElement).files ?? []);
  }

  submit(): void {
    this.busy.set(true);
    this.api
      .create({
        subject: this.subject.trim(),
        description: this.description,
        type: this.type,
        priority: this.priority,
        range_id: this.rangeId,
        exercise_id: this.exerciseId,
        queue_id: this.staff() ? this.queueId : null,
      })
      .pipe(
        switchMap(t =>
          this.files.length
            ? this.api.upload(t.id, this.files).pipe(
                switchMap(() => of(t)),
                catchError(() => {
                  this.notify.error('Ticket filed, but the attachments did not upload. Add them on the ticket.');
                  return of(t);
                }),
              )
            : of(t),
        ),
      )
      .subscribe({
        next: t => {
          this.notify.success(`${t.key} filed`);
          this.router.navigate(['/support', t.id]);
        },
        error: err => {
          this.busy.set(false);
          this.notify.error(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not file the ticket');
        },
      });
  }
}
