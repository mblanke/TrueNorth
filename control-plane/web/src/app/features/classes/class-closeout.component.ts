import { Component, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { AuthService } from '@core/services/auth.service';
import {
  AttendanceCell, AttendanceCorrection, AttendanceGrid, AttendanceStatus, ClassRecord, ClassesApiService, Closeout,
  ResultIn,
  ResultOutcome, Standing,
} from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { apiError } from '../ranges/range-capabilities';

export const ATTENDANCE_LABEL: Record<AttendanceStatus, string> = {
  present: 'Present', absent: 'Absent', excused: 'Excused',
};

export const OUTCOME_LABEL: Record<ResultOutcome, string> = {
  pass: 'Pass', fail: 'Fail', incomplete: 'Incomplete',
};

/** One Student's draft result on the close-out screen. */
export interface ResultDraft {
  outcome: ResultOutcome;
  comment: string;
}

/** The decision already made, else the suggestion: what the close-out screen starts from. */
export function draftsFrom(c: Closeout): Record<string, ResultDraft> {
  const out: Record<string, ResultDraft> = {};
  for (const s of c.students) out[s.entry_id] = { outcome: s.outcome ?? s.suggested, comment: s.comment ?? '' };
  return out;
}

/** The close request body: one result per Student, comments trimmed. */
export function toResults(drafts: Record<string, ResultDraft>): ResultIn[] {
  return Object.entries(drafts).map(([entry_id, d]) => ({
    entry_id, outcome: d.outcome, comment: d.comment.trim() || null,
  }));
}

/** How a Student stands on their course work: a number only where the deployment allows it. */
export function courseStanding(s: Standing, numbers: boolean): string {
  if (s.course_passed === null || s.course_passed === undefined) return 'No graded result yet';
  const verdict = s.course_passed ? 'Passed' : 'Below the mark';
  return numbers && s.course_percent !== null && s.course_percent !== undefined
    ? `${verdict} (${s.course_percent}%)` : verdict;
}

/**
 * A class's attendance grid and close-out (ADR 0014 §12-13).
 *
 * Attendance is recorded automatically after each session: present when the Student opened
 * their range during it. The lead or an admin corrects it to present or excused per session
 * (ADR 0014 §12), with an optional note (audited). Close-out pre-fills each Student's result from attendance and course work; the
 * lead sets pass, fail or incomplete with a comment and closes the class, which makes it
 * read-only. Only an admin reopens it. On an Unclassified deployment no number is shown.
 */
@Component({
  selector: 'tn-class-closeout',
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatButtonToggleModule, MatCardModule, MatFormFieldModule,
    MatIconModule, MatInputModule, MatSelectModule,
  ],
  template: `
    @if (grid(); as g) {
      <mat-card class="attendance">
        <mat-card-header>
          <mat-card-title>Attendance</mat-card-title>
          <mat-card-subtitle>Present: signed in and opened their range during the session.</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          @if (g.sessions.length === 0) {
            <p>No session has started yet.</p>
          } @else {
            @if (g.can_edit) {
              <p class="small">Correct a Student to present (P) or excused (E). Absent is only ever recorded by the clock.</p>
              <mat-form-field appearance="outline" class="note">
                <mat-label>Note for the next correction (optional, audited)</mat-label>
                <input matInput maxlength="500" [(ngModel)]="note" placeholder="e.g. medical appointment" />
              </mat-form-field>
            }
            <div class="scroll">
              <table class="entries grid">
                <thead>
                  <tr>
                    <th>Student</th>
                    @for (s of g.sessions; track s.session_id) {
                      <th>{{ s.name }}<br /><span class="small">{{ s.start_time | date:'EEE d MMM' }}</span></th>
                    }
                  </tr>
                </thead>
                <tbody>
                  @for (row of g.students; track row.entry_id) {
                    <tr>
                      <td>{{ row.display_name || row.email }}</td>
                      @for (cell of row.cells; track cell.session_id) {
                        <td [class]="'att-' + (cell.status ?? 'none')">
                          @if (g.can_edit) {
                            <mat-button-toggle-group hideSingleSelectionIndicator [value]="cell.status"
                              [attr.aria-label]="'Attendance of ' + (row.display_name || row.email)"
                              (change)="setAttendance(row.entry_id, cell.session_id, $event.value)">
                              <mat-button-toggle value="present" title="Present">P</mat-button-toggle>
                              <mat-button-toggle value="excused" title="Excused">E</mat-button-toggle>
                            </mat-button-toggle-group>
                            @if (cell.status === 'absent') { <span class="small">Absent</span> }
                          } @else {
                            {{ cellLabel(cell) }}
                          }
                          @if (cell.source === 'instructor') { <mat-icon class="tiny" title="Set by an instructor">edit</mat-icon> }
                          @if (cell.note) { <mat-icon class="tiny" [title]="cell.note" [attr.aria-label]="'Note: ' + cell.note">sticky_note_2</mat-icon> }
                        </td>
                      }
                    </tr>
                  }
                </tbody>
              </table>
            </div>
          }
        </mat-card-content>
      </mat-card>
    }

    @if (closeout(); as co) {
      <mat-card class="closeout">
        <mat-card-header>
          <mat-card-title>Close-out</mat-card-title>
          <mat-card-subtitle>
            @if (co.class_status === 'closed') {
              Closed {{ co.closed_at | date:'medium' }}. Read-only.
            } @else {
              Results are pre-filled from attendance and course work. You decide.
            }
          </mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          @if (!co.numbers_shown) { <p class="small">Unclassified: pass or fail only, no scores.</p> }
          <table class="entries results">
            <thead>
              <tr><th>Student</th><th>Attended</th><th>Course work</th>
                @if (co.numbers_shown) { <th>Detections</th> }
                <th>Suggested</th><th>Result</th><th>Comment</th></tr>
            </thead>
            <tbody>
              @for (s of co.students; track s.entry_id) {
                <tr>
                  <td>{{ s.display_name || s.email }}</td>
                  <td>{{ s.present + s.excused }} / {{ s.sessions_held }}
                    @if (s.excused) { <span class="small">({{ s.excused }} excused)</span> }</td>
                  <td>{{ standing(s, co.numbers_shown) }}</td>
                  @if (co.numbers_shown) { <td>{{ s.detections_credited ?? 0 }}</td> }
                  <td>{{ outcomeLabel(s.suggested) }}</td>
                  @if (editable(co) && drafts()[s.entry_id]; as d) {
                    <td>
                      <mat-select class="outcome" [ngModel]="d.outcome" [attr.aria-label]="'Result of ' + s.email"
                        (ngModelChange)="patch(s.entry_id, { outcome: $event })">
                        <mat-option value="pass">Pass</mat-option>
                        <mat-option value="fail">Fail</mat-option>
                        <mat-option value="incomplete">Incomplete</mat-option>
                      </mat-select>
                    </td>
                    <td>
                      <input class="input comment" maxlength="1000" [ngModel]="d.comment"
                        [attr.aria-label]="'Comment on ' + s.email" (ngModelChange)="patch(s.entry_id, { comment: $event })" />
                    </td>
                  } @else {
                    <td>{{ s.outcome ? outcomeLabel(s.outcome) : '—' }}</td>
                    <td>{{ s.comment ?? '' }}</td>
                  }
                </tr>
              } @empty { <tr><td colspan="7">Nobody is on the roll call.</td></tr> }
            </tbody>
          </table>
        </mat-card-content>
        <mat-card-actions align="end">
          <button mat-button (click)="exportCsv()"><mat-icon>download</mat-icon> Export CSV</button>
          @if (editable(co)) {
            <button mat-flat-button color="primary" (click)="close()">Close the class</button>
          }
          @if (co.class_status === 'closed' && auth.isAdmin()) {
            <mat-form-field appearance="outline" class="reason">
              <mat-label>Why reopen</mat-label>
              <input matInput [(ngModel)]="reopenReason" />
            </mat-form-field>
            <button mat-stroked-button [disabled]="reopenReason.trim().length < 3" (click)="reopen()">Reopen</button>
          }
        </mat-card-actions>
      </mat-card>
    }
  `,
  styles: [`
    .attendance, .closeout { margin-top: 16px; }
    .scroll { overflow-x: auto; }
    .note { width: 100%; max-width: 480px; }
    .entries { width: 100%; border-collapse: collapse; margin-top: 8px; }
    .entries th, .entries td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--mat-sys-outline-variant); vertical-align: middle; }
    .att-absent { color: var(--mat-sys-error); }
    .small { font-size: 12px; opacity: 0.8; }
    .tiny { font-size: 14px; width: 14px; height: 14px; vertical-align: middle; opacity: 0.7; }
    .outcome { width: 130px; }
    .comment { width: 100%; min-width: 160px; }
    .reason { width: 240px; margin-left: 8px; }
  `],
})
export class ClassCloseoutComponent {
  private readonly api = inject(ClassesApiService);
  private readonly notify = inject(NotificationService);
  readonly auth = inject(AuthService);

  readonly classRecord = input.required<ClassRecord>();
  /** The class changed state (closed or reopened): the page reloads its list. */
  readonly changed = output<void>();

  readonly grid = signal<AttendanceGrid | null>(null);
  readonly closeout = signal<Closeout | null>(null);
  readonly drafts = signal<Record<string, ResultDraft>>({});
  /** The note sent with the next attendance correction (ADR 0014 §12; audited). */
  note = '';
  reopenReason = '';

  constructor() {
    effect(() => this.load(this.classRecord().id));
  }

  load(id: string): void {
    this.api.attendance(id).subscribe({ next: g => this.grid.set(g), error: () => this.grid.set(null) });
    this.api.closeout(id).subscribe({
      next: c => { this.closeout.set(c); this.drafts.set(draftsFrom(c)); },
      error: () => this.closeout.set(null),
    });
  }

  editable(c: Closeout): boolean {
    return c.class_status === 'open' && this.classRecord().can_edit;
  }

  cellLabel(c: AttendanceCell): string { return c.status ? ATTENDANCE_LABEL[c.status] : '—'; }
  outcomeLabel(o: ResultOutcome): string { return OUTCOME_LABEL[o]; }
  standing(s: Standing, numbers: boolean): string { return courseStanding(s, numbers); }

  patch(entryId: string, p: Partial<ResultDraft>): void {
    this.drafts.update(d => ({ ...d, [entryId]: { ...d[entryId], ...p } }));
  }

  setAttendance(entryId: string, sessionId: string, status: AttendanceCorrection): void {
    const id = this.classRecord().id;
    this.api.setAttendance(id, sessionId, entryId, status, this.note.trim() || null).subscribe({
      next: () => { this.note = ''; this.load(id); },
      error: err => { this.notify.error(apiError(err)); this.load(id); },
    });
  }

  close(): void {
    const id = this.classRecord().id;
    this.api.close(id, toResults(this.drafts())).subscribe({
      next: c => {
        this.closeout.set(c);
        this.notify.success('Class closed: results sent to each Student\'s record');
        this.changed.emit();
        this.load(id);
      },
      error: err => this.notify.error(apiError(err)),
    });
  }

  reopen(): void {
    const id = this.classRecord().id;
    this.api.reopen(id, this.reopenReason.trim()).subscribe({
      next: () => { this.reopenReason = ''; this.notify.success('Class reopened'); this.changed.emit(); this.load(id); },
      error: err => this.notify.error(apiError(err)),
    });
  }

  exportCsv(): void {
    const c = this.classRecord();
    this.api.exportResults(c.id).subscribe({
      next: text => {
        const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
        const a = document.createElement('a');
        a.href = url;
        a.download = `${c.name.replace(/[^A-Za-z0-9_-]/g, '_')}-results.csv`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: err => this.notify.error(apiError(err)),
    });
  }
}
