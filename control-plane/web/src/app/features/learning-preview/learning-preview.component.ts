import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatButtonModule } from '@angular/material/button';

/** Isolated fictional previews: no application session or API access in the frames. */
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
          <a mat-flat-button color="primary" href="http://localhost:8083/"
             target="_blank" rel="noopener noreferrer">Open Moodle · new tab</a>
          <p>This address works on the Docker host. Moodle uses its own login.
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

  constructor() {
    inject(ActivatedRoute).queryParamMap.pipe(takeUntilDestroyed()).subscribe(params => {
      const view = params.get('view');
      this.view.set(view === 'readiness' || view === 'moodle' ? view : 'development');
    });
  }
}
