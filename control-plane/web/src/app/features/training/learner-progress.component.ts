import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CourseDetail, CourseEnrollment, CourseSummary, DirectoryUser, ModuleProgress, OperatorRecordsApiService,
} from '@core/services/operator-records-api.service';
import {
  ImpactDialogComponent, ImpactDialogData, ImpactDialogResult,
} from '../../shared/components/impact-dialog/impact-dialog.component';

function apiError(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (typeof err.error?.detail === 'string') return err.error.detail;
    if (err.status === 403) return 'Your role does not allow this.';
    if (err.status === 0) return 'The API could not be reached.';
    return `The API answered ${err.status}.`;
  }
  return 'The request failed.';
}

interface Opened {
  enrollment: CourseEnrollment;
  progress: ModuleProgress[] | null;
  error: string;
}

/**
 * A course's Students and each one's module progress (R0-B), with mark-complete for
 * instructors and admins (learning_record:read / learning_record:write in rbac.py). The
 * grade on completion is computed by the API from recorded progress; this screen never
 * supplies one, and shows the grade the API returns.
 */
@Component({
  selector: 'tn-learner-progress',
  imports: [CommonModule, FormsModule, MatButtonModule, MatCardModule, MatFormFieldModule, MatIconModule,
    MatSelectModule],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">fact_check</mat-icon>
          <div>
            <h1>Student progress</h1>
            <p class="subtitle">Each Student's progress through a course, module by module.</p>
          </div>
        </div>
      </div>

      @if (coursesError()) {
        <p class="error-text" role="alert">{{ coursesError() }}</p>
      } @else if (courses() === null) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading courses"></div>
      } @else if (courses()!.length === 0) {
        <p class="muted">No courses yet.</p>
      } @else {
        <mat-form-field appearance="outline" class="course-pick">
          <mat-label>Course</mat-label>
          <mat-select [ngModel]="courseId()" (ngModelChange)="pick($event)" panelClass="tn-select-panel">
            @for (c of courses()!; track c.id) {
              <mat-option [value]="c.id">{{ c.name }}{{ c.is_published ? '' : ' (draft)' }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
      }

      @if (courseId()) {
        <mat-card class="panel">
          @if (outcome()) { <p class="outcome" role="status" aria-live="polite">{{ outcome() }}</p> }
          @if (usersError()) { <p class="muted">{{ usersError() }}</p> }
          @if (enrollmentsError()) {
            <p class="error-text" role="alert">{{ enrollmentsError() }}</p>
          } @else if (enrollments() === null) {
            <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
          } @else if (enrollments()!.length === 0) {
            <p class="muted">No Students in your organisation are enrolled on this course.</p>
          } @else {
            <table class="tn-table">
              <caption class="sr-only">Students enrolled on {{ course()?.name }}</caption>
              <thead><tr><th scope="col">Student</th><th scope="col">Status</th><th scope="col">Enrolled</th>
                <th scope="col">Completed</th><th scope="col">Score</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>
                @for (e of enrollments()!; track e.id) {
                  <tr>
                    <td>{{ nameOf(e.user_id) }}</td>
                    <td><span class="status-chip" [class]="e.status">{{ e.status }}</span></td>
                    <td>{{ e.enrolled_at | date:'mediumDate' }}</td>
                    <td>{{ e.completed_at ? (e.completed_at | date:'mediumDate') : '—' }}</td>
                    <td>{{ e.status === 'completed' ? (e.final_score + ' / ' + e.max_score + (e.final_grade ? ' (' + e.final_grade + ')' : '')) : '—' }}</td>
                    <td class="actions">
                      <button mat-stroked-button (click)="toggle(e)" [attr.aria-expanded]="opened()?.enrollment?.id === e.id"
                              [attr.aria-controls]="'progress-' + e.id">
                        {{ opened()?.enrollment?.id === e.id ? 'Hide' : 'Progress' }}
                      </button>
                      @if (canComplete() && e.status !== 'completed') {
                        <button mat-stroked-button color="primary" (click)="complete(e)" [disabled]="busy()"
                                [attr.aria-label]="'Mark ' + nameOf(e.user_id) + ' complete'">Mark complete</button>
                      }
                    </td>
                  </tr>
                  @if (opened()?.enrollment?.id === e.id) {
                    <tr [id]="'progress-' + e.id">
                      <td colspan="6" class="progress-cell">
                        @if (opened()!.error) {
                          <p class="error-text" role="alert">{{ opened()!.error }}</p>
                        } @else if (opened()!.progress === null) {
                          <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
                        } @else if (opened()!.progress!.length === 0) {
                          <p class="muted">No module started yet.</p>
                        } @else {
                          <table class="tn-table inner">
                            <caption class="sr-only">Module progress of {{ nameOf(e.user_id) }}</caption>
                            <thead><tr><th scope="col">Module</th><th scope="col">Status</th><th scope="col">Score</th>
                              <th scope="col">Attempts</th><th scope="col">Last activity</th></tr></thead>
                            <tbody>
                              @for (p of opened()!.progress!; track p.id) {
                                <tr>
                                  <td>{{ moduleTitle(p.module_id) }}</td>
                                  <td>{{ p.status }}</td>
                                  <td>{{ p.score }} / {{ p.max_score }}</td>
                                  <td>{{ p.attempts }}</td>
                                  <td>{{ (p.completed_at || p.last_accessed_at) ? ((p.completed_at || p.last_accessed_at) | date:'short') : '—' }}</td>
                                </tr>
                              }
                            </tbody>
                          </table>
                        }
                      </td>
                    </tr>
                  }
                }
              </tbody>
            </table>
          }
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .course-pick { width: 100%; max-width: 560px; margin-top: 16px; }
    .panel { padding: 16px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .error-text { color: var(--alert, #ef4444); }
    .outcome { border-left: 4px solid var(--accent); padding: 6px 10px; background: var(--bg-surface); }
    .tn-table { width: 100%; border-collapse: collapse; }
    .tn-table th, .tn-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .inner { margin: 4px 0 8px; font-size: 13px; }
    .progress-cell { background: var(--bg-surface); }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class LearnerProgressComponent implements OnInit {
  private api = inject(OperatorRecordsApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);

  courses = signal<CourseSummary[] | null>(null);
  coursesError = signal('');
  courseId = signal('');
  course = signal<CourseDetail | null>(null);
  enrollments = signal<CourseEnrollment[] | null>(null);
  enrollmentsError = signal('');
  users = signal<Map<string, DirectoryUser>>(new Map());
  usersError = signal('');
  opened = signal<Opened | null>(null);
  busy = signal(false);
  outcome = signal('');

  /** learning_record:write: instructor and admin (rbac.py). The route already requires one. */
  readonly canComplete = computed(() => {
    const role = this.auth.user()?.role;
    return role === 'admin' || role === 'instructor';
  });

  ngOnInit(): void {
    this.api.listCourses().subscribe({
      next: page => this.courses.set(page.items),
      error: err => this.coursesError.set(apiError(err)),
    });
    this.api.listUsers().subscribe({
      next: list => this.users.set(new Map(list.map(u => [u.id, u]))),
      error: err => this.usersError.set(`Names could not be loaded (${apiError(err)}); Students are shown by id.`),
    });
  }

  nameOf(userId: string): string {
    const u = this.users().get(userId);
    return u ? `${u.display_name} (${u.email})` : userId;
  }

  moduleTitle(moduleId: string): string {
    const m = this.course()?.modules?.find(x => x.id === moduleId);
    return m ? `${m.ordinal}. ${m.title}` : moduleId;
  }

  pick(courseId: string): void {
    this.courseId.set(courseId);
    this.opened.set(null);
    this.outcome.set('');
    this.course.set(null);
    this.api.getCourse(courseId).subscribe({ next: c => this.course.set(c), error: () => this.course.set(null) });
    this.loadEnrollments();
  }

  loadEnrollments(): void {
    this.enrollments.set(null);
    this.enrollmentsError.set('');
    this.api.listEnrollments(this.courseId()).subscribe({
      next: e => this.enrollments.set(e),
      error: err => this.enrollmentsError.set(apiError(err)),
    });
  }

  toggle(e: CourseEnrollment): void {
    if (this.opened()?.enrollment.id === e.id) {
      this.opened.set(null);
      return;
    }
    this.opened.set({ enrollment: e, progress: null, error: '' });
    this.api.getProgress(e.course_id, e.user_id).subscribe({
      next: p => this.opened.set({ enrollment: e, progress: p, error: '' }),
      error: err => this.opened.set({ enrollment: e, progress: null, error: apiError(err) }),
    });
  }

  complete(e: CourseEnrollment): void {
    const who = this.nameOf(e.user_id);
    const data: ImpactDialogData = {
      title: `Mark ${who} complete?`,
      impact: [
        'The enrollment is recorded as completed now, whatever modules remain.',
        "The final score and letter grade are computed by the API from the Student's recorded module progress; you do not enter them.",
        'This is recorded in the audit log under your name.',
      ],
      confirmText: 'Mark complete',
    };
    this.dialog
      .open<ImpactDialogComponent, ImpactDialogData, ImpactDialogResult | null>(ImpactDialogComponent, {
        width: '520px', data,
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.busy.set(true);
        this.api.markComplete(e.course_id, e.user_id).subscribe({
          next: out => {
            this.busy.set(false);
            const grade = out.final_grade ? `grade ${out.final_grade}` : 'no letter grade (nothing graded)';
            this.outcome.set(`${who}: ${out.status}, ${out.final_score} / ${out.max_score}, ${grade}.`);
            this.loadEnrollments();
          },
          error: err => {
            this.busy.set(false);
            const msg = apiError(err);
            this.outcome.set(`Not completed: ${msg}`);
            this.notify.error(msg);
          },
        });
      });
  }
}
