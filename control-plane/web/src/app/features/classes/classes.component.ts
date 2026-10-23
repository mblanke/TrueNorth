import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { AuthService } from '@core/services/auth.service';
import {
  ClassIn, ClassRecord, ClassesApiService, ReinstateResult, RollCallAddResult, RollCallEntry, RollCallStatus,
} from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CourseSummary, DirectoryUser, OperatorRecordsApiService,
} from '@core/services/operator-records-api.service';
import { roleCan } from '@core/auth/permissions';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { apiError } from '../ranges/range-capabilities';

export interface ClassForm {
  name: string;
  courseId: string;
  start: string; // yyyy-mm-dd
  end: string;
  leadId: string; // '' = the caller
  assistantIds: string[];
  observerIds: string[];
}

export const STATUS_LABEL: Record<RollCallStatus, string> = {
  invited: 'Invited', signed_in: 'Signed in', ready: 'Ready', removed: 'Removed',
  pending: 'Awaiting approval', forfeited: 'Lost seat',
};

/** What a reinstatement did, in one line. */
export function reinstateSummary(email: string, r: ReinstateResult): string {
  if (r.status === 'pending') {
    return `${email} is back, but the approved sessions have no room: an approver decides (${r.pending_reason ?? ''}).`;
  }
  return `${email} has their seat back (${STATUS_LABEL[r.status].toLowerCase()}).`;
}

/** The request body for a form, or why it cannot be sent yet. */
export function toClassBody(f: ClassForm): ClassIn | string {
  if (f.name.trim().length < 2) return 'Give the class a name or serial.';
  if (!f.courseId) return 'Choose a course.';
  if (!f.start || !f.end) return 'Set the start and end dates.';
  if (f.end < f.start) return 'The end date must be on or after the start date.';
  return {
    name: f.name.trim(), course_id: f.courseId, start_date: f.start, end_date: f.end,
    lead_instructor_id: f.leadId || null, assistant_ids: f.assistantIds, observer_ids: f.observerIds,
  };
}

/** One line saying what an add did. */
export function addSummary(r: RollCallAddResult): string {
  const parts = [`Added ${r.added.length}`];
  if (r.already_on_roll_call.length) parts.push(`already on it: ${r.already_on_roll_call.length}`);
  if (r.refused.length) parts.push(`refused: ${r.refused.map(x => `${x.email} (${x.reason})`).join(', ')}`);
  if (r.invalid.length) parts.push(`not email addresses: ${r.invalid.join(', ')}`);
  if (r.not_emailed?.length) parts.push(`not emailed: ${r.not_emailed.map(x => `${x.email} (${x.reason})`).join(', ')}`);
  // Added after the sessions were approved, with no room left: an approver decides (ADR 0014 §17).
  if (r.pending_approval?.length) parts.push(`awaiting approval (no room in the approved sessions): ${r.pending_approval.map(x => x.email).join(', ')}`);
  return parts.join('; ') + '.';
}

/**
 * The staff Classes page (ADR 0014): the classes you lead, assist or observe (an admin
 * sees every class of the organisation), creating one, and its roll call by paste or CSV.
 * A Student on a roll call who has no account yet is emailed to onboard, and goes straight
 * in on their first AD sign-in. The calendar is a later page.
 *
 * A Student not signed in by 2 hours after the class's first approved session started loses
 * their seat (ADR 0014 §4): shown as "Lost seat", with Reinstate for the lead or an admin.
 */
@Component({
  selector: 'tn-classes',
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatCardModule, MatChipsModule, MatFormFieldModule,
    MatIconModule, MatInputModule, MatSelectModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">groups</mat-icon>
          <div>
            <h1>Classes</h1>
            <p class="subtitle">A Student on a class's roll call signs in with AD and goes straight in.</p>
          </div>
        </div>
        @if (canCreate() && !form()) {
          <button mat-flat-button color="primary" (click)="startCreate()"><mat-icon>add</mat-icon> New class</button>
        }
      </div>

      @if (form(); as f) {
        <mat-card class="form-card">
          <mat-card-header><mat-card-title>New class</mat-card-title></mat-card-header>
          <mat-card-content class="form-grid">
            <mat-form-field appearance="outline">
              <mat-label>Name or serial</mat-label>
              <input matInput [ngModel]="f.name" (ngModelChange)="patch({ name: $event })" placeholder="C201-26-03" />
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Course</mat-label>
              <mat-select [ngModel]="f.courseId" (ngModelChange)="patch({ courseId: $event })">
                @for (c of courses(); track c.id) { <mat-option [value]="c.id">{{ c.name }}</mat-option> }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Start date</mat-label>
              <input matInput type="date" [ngModel]="f.start" (ngModelChange)="patch({ start: $event })" />
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>End date</mat-label>
              <input matInput type="date" [ngModel]="f.end" (ngModelChange)="patch({ end: $event })" />
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Lead instructor</mat-label>
              <mat-select [ngModel]="f.leadId" (ngModelChange)="patch({ leadId: $event })">
                @if (!auth.isAdmin()) { <mat-option value="">Me</mat-option> }
                @for (u of instructors(); track u.id) { <mat-option [value]="u.id">{{ u.display_name }}</mat-option> }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Assistant instructors</mat-label>
              <mat-select multiple [ngModel]="f.assistantIds" (ngModelChange)="patch({ assistantIds: $event })">
                @for (u of instructors(); track u.id) { <mat-option [value]="u.id">{{ u.display_name }}</mat-option> }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Observers</mat-label>
              <mat-select multiple [ngModel]="f.observerIds" (ngModelChange)="patch({ observerIds: $event })">
                @for (u of observers(); track u.id) { <mat-option [value]="u.id">{{ u.display_name }}</mat-option> }
              </mat-select>
            </mat-form-field>
          </mat-card-content>
          @if (formError()) { <p class="error" role="alert">{{ formError() }}</p> }
          <mat-card-actions align="end">
            <button mat-button (click)="form.set(null)">Cancel</button>
            <button mat-flat-button color="primary" (click)="save()">Create</button>
          </mat-card-actions>
        </mat-card>
      }

      @if (classes().length === 0 && !loading()) {
        <tn-empty-state icon="groups" title="No classes"
          message="Classes you lead, assist or observe appear here." />
      }

      <div class="class-list">
        @for (c of classes(); track c.id) {
          <mat-card class="class-card" [class.selected]="selected()?.id === c.id">
            <mat-card-header>
              <mat-card-title>{{ c.name }}</mat-card-title>
              <mat-card-subtitle>
                {{ c.course_name }} · {{ c.start_date | date:'mediumDate' }} – {{ c.end_date | date:'mediumDate' }}
              </mat-card-subtitle>
            </mat-card-header>
            <mat-card-content>
              <p>Lead: {{ c.lead_instructor?.display_name ?? '—' }} · You: {{ c.my_role ?? '—' }}
                · On the roll call: {{ c.roll_call_count }}</p>
            </mat-card-content>
            <mat-card-actions>
              <button mat-button (click)="open(c)"><mat-icon>fact_check</mat-icon> Roll call</button>
            </mat-card-actions>
          </mat-card>
        }
      </div>

      @if (selected(); as c) {
        <mat-card class="roll-call">
          <mat-card-header>
            <mat-card-title>Roll call: {{ c.name }}</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            @if (c.can_edit) {
              <mat-form-field appearance="outline" class="paste">
                <mat-label>Paste Students' email addresses</mat-label>
                <textarea matInput rows="4" [(ngModel)]="pasted"
                  placeholder="one per line, or separated by commas"></textarea>
              </mat-form-field>
              <div class="add-row">
                <button mat-flat-button color="primary" [disabled]="!pasted.trim()" (click)="addPasted()">Add</button>
                <label class="csv">
                  <input type="file" accept=".csv,text/csv" (change)="addCsv($event)" />
                  or upload a CSV (an <code>email</code> column)
                </label>
              </div>
              @if (lastAdd()) { <p class="summary" role="status">{{ lastAdd() }}</p> }
            }
            <table class="entries">
              <thead><tr><th>Email</th><th>Name</th><th>Status</th><th></th></tr></thead>
              <tbody>
                @for (e of rollCall(); track e.id) {
                  <tr [class.removed]="e.status === 'removed'" [class.forfeited]="e.status === 'forfeited'">
                    <td>{{ e.email }}</td>
                    <td>{{ e.display_name ?? '' }}</td>
                    <td>
                      <mat-chip [class]="'status-' + e.status">{{ label(e) }}</mat-chip>
                      @if (e.status === 'forfeited' && e.forfeited_at) {
                        <span class="when">not signed in by {{ e.forfeited_at | date:'short' }}</span>
                      }
                    </td>
                    <td>
                      @if (c.can_edit && e.status === 'forfeited') {
                        <button mat-button color="primary" (click)="reinstate(e)">Reinstate</button>
                      }
                      @if (c.can_edit && e.status !== 'removed') {
                        <button mat-button color="warn" (click)="remove(e)">Remove</button>
                      }
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </mat-card-content>
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0 16px; }
    .class-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin: 16px 0; }
    .class-card.selected { outline: 2px solid var(--mat-sys-primary); }
    .paste { width: 100%; }
    .add-row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
    .entries { width: 100%; border-collapse: collapse; margin-top: 16px; }
    .entries th, .entries td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--mat-sys-outline-variant); }
    .entries tr.removed td { opacity: 0.6; }
    .entries tr.forfeited td { color: var(--mat-sys-error); }
    .when { margin-left: 8px; font-size: 12px; }
    .error { color: var(--mat-sys-error); padding: 0 16px; }
  `],
})
export class ClassesComponent implements OnInit {
  private readonly api = inject(ClassesApiService);
  private readonly records = inject(OperatorRecordsApiService);
  private readonly notify = inject(NotificationService);
  readonly auth = inject(AuthService);

  readonly classes = signal<ClassRecord[]>([]);
  readonly loading = signal(true);
  readonly selected = signal<ClassRecord | null>(null);
  readonly rollCall = signal<RollCallEntry[]>([]);
  readonly form = signal<ClassForm | null>(null);
  readonly formError = signal('');
  readonly lastAdd = signal('');
  readonly courses = signal<CourseSummary[]>([]);
  readonly users = signal<DirectoryUser[]>([]);
  readonly instructors = computed(() => this.users().filter(u => u.role === 'instructor' || u.role === 'admin'));
  readonly observers = computed(() => this.users().filter(u => u.role !== 'student' && u.role !== 'range_ops'));
  readonly canCreate = computed(() => roleCan(this.auth.user()?.role, 'class:create'));
  pasted = '';

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    const source = this.auth.isAdmin() ? this.api.list() : this.api.mine();
    source.subscribe({
      next: rows => { this.classes.set(rows); this.loading.set(false); },
      error: err => { this.loading.set(false); this.notify.error(apiError(err)); },
    });
  }

  label(e: RollCallEntry): string {
    return STATUS_LABEL[e.status];
  }

  startCreate(): void {
    this.formError.set('');
    this.form.set({ name: '', courseId: '', start: '', end: '', leadId: '', assistantIds: [], observerIds: [] });
    this.records.listCourses().subscribe({ next: p => this.courses.set(p.items), error: () => {} });
    this.records.listUsers().subscribe({ next: u => this.users.set(u), error: () => {} });
  }

  patch(p: Partial<ClassForm>): void {
    this.form.update(f => (f ? { ...f, ...p } : f));
  }

  save(): void {
    const f = this.form();
    if (!f) return;
    const body = toClassBody(f);
    if (typeof body === 'string') { this.formError.set(body); return; }
    this.api.create(body).subscribe({
      next: c => {
        this.form.set(null);
        this.notify.success(`Class ${c.name} created`);
        this.load();
        this.open(c);
      },
      error: err => this.formError.set(apiError(err)),
    });
  }

  open(c: ClassRecord): void {
    this.selected.set(c);
    this.lastAdd.set('');
    this.api.rollCall(c.id).subscribe({
      next: rows => this.rollCall.set(rows),
      error: err => this.notify.error(apiError(err)),
    });
  }

  private added(c: ClassRecord, r: RollCallAddResult): void {
    this.open(c);
    this.lastAdd.set(addSummary(r));
    this.load();
  }

  addPasted(): void {
    const c = this.selected();
    if (!c || !this.pasted.trim()) return;
    this.api.addPasted(c.id, this.pasted).subscribe({
      next: r => { this.pasted = ''; this.added(c, r); },
      error: err => this.notify.error(apiError(err)),
    });
  }

  addCsv(event: Event): void {
    const c = this.selected();
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!c || !file) return;
    this.api.addCsv(c.id, file).subscribe({
      next: r => { input.value = ''; this.added(c, r); },
      error: err => this.notify.error(apiError(err)),
    });
  }

  reinstate(e: RollCallEntry): void {
    const c = this.selected();
    if (!c) return;
    this.api.reinstate(c.id, e.id).subscribe({
      next: r => {
        this.notify.success(reinstateSummary(e.email, r));
        this.open(c);
        this.load();
      },
      error: err => this.notify.error(apiError(err)),
    });
  }

  remove(e: RollCallEntry): void {
    const c = this.selected();
    if (!c) return;
    this.api.remove(c.id, e.id).subscribe({
      next: () => this.open(c),
      error: err => this.notify.error(apiError(err)),
    });
  }
}
