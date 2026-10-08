import { Component, computed, inject, input, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatButtonModule } from '@angular/material/button';
import { environment } from '@env/environment';
import { AuthService } from '@core/services/auth.service';
import { CourseStudioApiService, LearningPlatform } from '@core/services/course-studio-api.service';

/** Only absolute http(s) URLs or same-origin paths are offered as the Moodle link. */
function httpUrl(value: string | null | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed && (/^https?:\/\/\S+$/i.test(trimmed) || /^\/(?!\/)\S*$/.test(trimmed)) ? trimmed : undefined;
}

/** The base URL of the tenant's first active registered Moodle, if any. */
export function registeredMoodleUrl(platforms: readonly LearningPlatform[]): string | undefined {
  const moodle = platforms.find(p => p.is_active && p.platform_type === 'moodle' && httpUrl(p.base_url));
  return httpUrl(moodle?.base_url);
}

/**
 * Isolated fictional previews: no application session or API access in the frames.
 *
 * The Moodle link is never hard-coded (it used to be http://localhost:8083/, which is
 * only right on a developer's Docker host). It comes from, in order:
 *   1. the Moodle platform registered under Integrations (GET /integrations/platforms,
 *      `integration:read`, so only instructors and admins look it up);
 *   2. the `moodleUrl` input (route data via withComponentInputBinding, or a host);
 *   3. the deployment's runtime config: `environment.moodleUrl`, set from TN_MOODLE_URL
 *      through assets/config.json (core/config/runtime-config.ts) before bootstrap.
 * With none, the panel says no Moodle is registered instead of linking anywhere.
 */
@Component({
  selector: 'tn-learning-preview',
  imports: [RouterLink, MatButtonModule],
  template: `
    <section aria-label="LMS and mission readiness test workspace">
      <nav aria-label="Test workspaces">
        <a mat-stroked-button routerLink="." [queryParams]="{view: 'development'}"
           [attr.aria-current]="view() === 'development' ? 'page' : null">LMS development</a>
        <a mat-stroked-button routerLink="." [queryParams]="{view: 'readiness'}"
           [attr.aria-current]="view() === 'readiness' ? 'page' : null">Commander readiness</a>
        <a mat-stroked-button routerLink="." [queryParams]="{view: 'moodle'}"
           [attr.aria-current]="view() === 'moodle' ? 'page' : null">Moodle</a>
      </nav>
      @if (view() === 'moodle') {
        <article>
          <h2>Moodle test LMS</h2>
          <p>Moodle runs as a separate service on this test machine. Open it to view
            its courses, activities, enrolment, and gradebook.</p>
          @if (moodleLink(); as link) {
            <a mat-flat-button color="primary" [href]="link"
               target="_blank" rel="noopener noreferrer">Open Moodle · new tab</a>
          } @else {
            <p class="unconfigured">No Moodle site is registered for this deployment. An
              administrator registers one under TrueNorth integration settings.</p>
          }
          <p>Moodle uses its own login.
            Starting Moodle does not establish LTI launch, grade passback, or cmi5 integration.</p>
          <a mat-stroked-button routerLink="/integrations">TrueNorth integration settings</a>
        </article>
      } @else {
        <p class="notice">Interactive design preview · Fictional records · Actions do not
          enrol learners, task a team, run an exercise, or change training records.</p>
        @if (view() === 'readiness') {
          <iframe src="assets/previews/readiness.html" title="Commander readiness design preview"
                  sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
        } @else {
          <iframe src="assets/previews/development.html" title="LMS development design preview"
                  sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
        }
      }
    </section>
  `,
  styles: [`
    nav { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
    a[aria-current="page"] { border: 2px solid var(--accent); }
    .notice { color: var(--text-secondary); margin: 0 0 12px; }
    iframe { display: block; width: 100%; height: 80vh; min-height: 600px; border: 0; }
    article { max-width: 750px; padding: 24px; border: 1px solid var(--border); border-radius: 12px; }
    article p { margin: 16px 0; }
  `],
})
export class LearningPreviewComponent {
  readonly view = signal('development');
  /** Fallback Moodle address when no Moodle platform is registered. */
  readonly moodleUrl = input<string | undefined>(undefined);
  private readonly registered = signal<string | undefined>(undefined);
  readonly moodleLink = computed(
    () => this.registered() ?? httpUrl(this.moodleUrl()) ?? httpUrl(environment.moodleUrl),
  );

  constructor() {
    if (inject(AuthService).isInstructor()) {
      inject(CourseStudioApiService).platforms().pipe(takeUntilDestroyed()).subscribe({
        next: platforms => this.registered.set(registeredMoodleUrl(platforms)),
        error: () => this.registered.set(undefined),
      });
    }

    inject(ActivatedRoute).queryParamMap.pipe(takeUntilDestroyed()).subscribe(params => {
      const view = params.get('view');
      this.view.set(view === 'readiness' || view === 'moodle' ? view : 'development');
    });
  }
}
