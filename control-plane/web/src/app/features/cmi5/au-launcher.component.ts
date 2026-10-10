import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';

import { Cmi5ApiService, Cmi5Au, Cmi5Structure } from '@core/services/cmi5-api.service';

/**
 * A released course's assignable units, launched with TrueNorth as the cmi5 LMS:
 * `/au/releases/:releaseId`. It is also where an AU's Exit (returnURL) brings the Student
 * back to, with the AU's new status.
 */
@Component({
  selector: 'tn-cmi5-au-launcher',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatCardModule, MatProgressBarModule],
  template: `
    <div class="launcher">
      <mat-card class="panel">
        @if (structure(); as s) {
          <h1>{{ s.title }}</h1>
          @if (s.course_satisfied) {
            <p class="done" data-testid="course-satisfied">Course complete: every module's requirement is met.</p>
          }
          @if (!s.enrolled) {
            <p class="muted">You are not enrolled on this version of the course, so modules cannot be launched here.</p>
          }
          <ol class="aus">
            @for (au of s.aus; track au.index) {
              <li [attr.data-testid]="'au-' + au.index">
                <div class="what">
                  <b>{{ au.title }}</b>
                  <span class="muted">{{ requirement(au) }} · {{ state(au) }}</span>
                </div>
                @if (s.enrolled) {
                  <button mat-raised-button color="primary" (click)="launch(au)" [disabled]="busy()"
                          [attr.data-testid]="'launch-' + au.index">
                    {{ au.satisfied && au.move_on !== 'NotApplicable' ? 'Review' : 'Launch' }}
                  </button>
                }
              </li>
            }
          </ol>
        } @else if (problem()) {
          <p class="error">{{ problem() }}</p>
        } @else {
          <mat-progress-bar mode="indeterminate" />
        }
        @if (problem() && structure()) {
          <p class="error">{{ problem() }}</p>
        }
      </mat-card>
    </div>
  `,
  styles: [`
    .launcher { display: flex; justify-content: center; padding: 32px 16px; }
    .panel { width: min(820px, 100%); padding: 24px; border: 1px solid var(--border); }
    h1 { margin: 0 0 12px; font-size: 22px; }
    .aus { padding-left: 20px; display: flex; flex-direction: column; gap: 12px; }
    li { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .what { display: flex; flex-direction: column; }
    .muted { color: var(--text-muted); }
    .done { font-weight: 600; }
    .error { color: var(--severity-high); }
  `],
})
export class Cmi5LauncherComponent implements OnInit {
  private readonly api = inject(Cmi5ApiService);
  private readonly route = inject(ActivatedRoute);
  /** Where a launch goes; a seam for the unit specs. */
  static open: (url: string) => void = url => window.location.assign(url);

  readonly structure = signal<Cmi5Structure | null>(null);
  readonly problem = signal('');
  readonly busy = signal(false);
  private releaseId = '';

  ngOnInit(): void {
    this.releaseId = this.route.snapshot.paramMap.get('releaseId') ?? '';
    // `?launch=<n>`: go straight into module n. This is the target of an LTI link from
    // Moodle (docs/cmi5.md, "Moodle"): the Student arrives signed in and the module opens,
    // with TrueNorth as its cmi5 LMS.
    const wanted = this.route.snapshot.queryParamMap?.get('launch') ?? null;
    const autoLaunch = wanted !== null && /^\d+$/.test(wanted) ? Number(wanted) : null;
    this.api.structure(this.releaseId).subscribe({
      next: s => {
        this.structure.set(s);
        const au = autoLaunch === null ? undefined : s.aus.find(a => a.index === autoLaunch);
        if (autoLaunch !== null && !au) {
          this.problem.set(`This course has no module ${autoLaunch + 1}.`);
        } else if (au && s.enrolled) {
          this.launch(au);
        }
      },
      error: err => this.problem.set(err?.error?.detail ?? 'This course could not be loaded.'),
    });
  }

  requirement(au: Cmi5Au): string {
    switch (au.move_on) {
      case 'Passed':
        return au.mastery_score === null || au.mastery_score === undefined
          ? 'Pass the quiz'
          : `Pass the quiz (${Math.round(au.mastery_score * 100)}%)`;
      case 'Completed':
        return 'Read it to the end';
      case 'CompletedAndPassed':
        return 'Read it and pass the quiz';
      case 'CompletedOrPassed':
        return 'Read it or pass the quiz';
      default:
        return 'Optional';
    }
  }

  state(au: Cmi5Au): string {
    if (au.waived) return `Waived (${au.waived})`;
    if (au.satisfied && au.move_on !== 'NotApplicable') return 'Done';
    if (au.passed) return 'Passed';
    if (au.completed) return 'Read';
    return 'Not started';
  }

  launch(au: Cmi5Au): void {
    this.busy.set(true);
    this.problem.set('');
    this.api.launch(this.releaseId, au.index).subscribe({
      next: out => Cmi5LauncherComponent.open(out.url),
      error: err => {
        this.busy.set(false);
        this.problem.set(err?.error?.detail ?? 'The module could not be launched.');
      },
    });
  }
}
