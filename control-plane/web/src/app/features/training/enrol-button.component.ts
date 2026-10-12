import { ChangeDetectionStrategy, Component, Injectable, computed, inject, input, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ApiService, Enrollment } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';

/**
 * The signed-in Student's own course enrolments, as the server has them.
 *
 * Loaded once from GET /courses/enrollments/mine and updated from each enrolment the
 * server confirms; never from a click alone. Shared by the catalogue's rows and the
 * course page, so both say the same thing.
 */
@Injectable({ providedIn: 'root' })
export class EnrolmentsService {
  private readonly api = inject(ApiService);
  private readonly ids = signal<ReadonlySet<string>>(new Set());
  /** 'error': we do not know; the buttons still work (the server answers a repeat with 200). */
  readonly status = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');

  load(): void {
    this.status.set('loading');
    this.api.myEnrollments().subscribe({
      next: rows => { this.ids.set(new Set(rows.map(r => r.course_id))); this.status.set('ready'); },
      error: () => this.status.set('error'),
    });
  }

  isEnrolled(courseId: string): boolean {
    return this.ids().has(courseId);
  }

  /** POST /courses/{id}/enroll for the caller. 201 new, 200 already enrolled: both count. */
  enrol(courseId: string, userId: string): Observable<Enrollment> {
    return this.api.enrollInCourse(courseId, userId).pipe(
      tap(e => this.ids.update(s => new Set(s).add(e.course_id))),
    );
  }
}

/**
 * Enrol on one course. Disabled while its request is in flight, so a double click sends
 * one request (and the server is idempotent anyway). A refusal shows the server's reason:
 * a draft or another tenant's course is "not found", a retired one says so.
 */
@Component({
  selector: 'tn-enrol-button',
  imports: [MatButtonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (userId()) {
      @if (enrolled()) {
        <span class="enrolled" data-testid="enrolled"><mat-icon aria-hidden="true">check_circle</mat-icon> Enrolled</span>
      } @else {
        <button mat-stroked-button type="button" data-testid="enrol"
                [disabled]="busy()" [attr.aria-label]="'Enrol on ' + courseName()"
                (click)="enrol()">
          {{ busy() ? 'Enrolling…' : 'Enrol' }}
        </button>
      }
      @if (error(); as e) { <span class="err" role="alert">{{ e }}</span> }
    }
  `,
  styles: [`
    :host { display: inline-flex; align-items: center; gap: 8px; }
    .enrolled { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; color: var(--success); }
    .enrolled mat-icon { font-size: 18px; width: 18px; height: 18px; }
    .err { font-size: 12px; color: var(--danger, var(--warning)); }
  `],
})
export class EnrolButtonComponent {
  private readonly auth = inject(AuthService);
  private readonly enrolments = inject(EnrolmentsService);

  readonly courseId = input.required<string>();
  readonly courseName = input('this course');

  protected readonly userId = computed(() => this.auth.userId());
  protected readonly enrolled = computed(() => this.enrolments.isEnrolled(this.courseId()));
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  enrol(): void {
    const userId = this.userId();
    if (!userId || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    this.enrolments.enrol(this.courseId(), userId).subscribe({
      next: () => this.busy.set(false),
      error: (err: { status?: number; error?: { detail?: string } }) => {
        this.busy.set(false);
        this.error.set(
          err?.status === 404 ? 'This course is not open for enrolment.'
            : err?.error?.detail ?? 'Enrolment failed. Try again.',
        );
      },
    });
  }
}
