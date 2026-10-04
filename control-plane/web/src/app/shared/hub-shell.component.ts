import { Component, computed, inject } from '@angular/core';
import {
  ActivatedRoute,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import { QuietStylesComponent } from './quiet-styles.component';

interface HubTab {
  label: string;
  path: string;
  /** Shown only to instructors and admins. The route must still carry its own guard;
   * hiding the tab is presentation, not access control. */
  instructorOnly?: boolean;
}

/**
 * Generic tabbed hub shell. A parent route supplies `title` and `tabs` via route
 * `data`; each tab is a child route rendered in the outlet below the tab bar.
 * Lets several existing feature screens be consolidated under one nav entry.
 */
@Component({
  selector: 'tn-hub-shell',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, QuietStylesComponent],
  template: `
    <tn-quiet-styles />
    <section class="hub">
      @if (title) {
        <header class="hub-heading">
          <p class="hub-eyebrow">TrueNorth / {{ title }}</p>
          <h1 class="hub-title">{{ title }}</h1>
          <p class="hub-description">{{ description }}</p>
        </header>
      }
      <nav class="hub-tabs" [attr.aria-label]="title + ' sections'">
        @for (tab of visibleTabs(); track tab.path) {
          <a
            [routerLink]="tab.path"
            routerLinkActive="selected"
            ariaCurrentWhenActive="page"
          >
            {{ tab.label }}
          </a>
        }
      </nav>
      <div class="hub-panel">
        <router-outlet />
      </div>
    </section>
  `,
  styles: [
    `
      .hub {
        display: flex;
        flex-direction: column;
        min-height: 0;
        padding: 28px;
        max-width: 1400px;
        margin: 0 auto;
      }
      .hub-title {
        margin: 0 0 0.5rem;
        font-size: 25px;
        font-weight: 600;
      }
      .hub-tabs {
        display: flex; flex-wrap: wrap; gap: 6px;
        margin: 24px 0; padding-bottom: 10px;
        border-bottom: 1px solid var(--border);
      }
      .hub-eyebrow { font-size: 12px; color: var(--text-muted); margin: 0 0 8px; }
      .hub-description { color: var(--text-muted); margin: 0; font-size: 14px; }
      .hub-tabs a { padding: 8px 12px; border-radius: 5px; color: var(--text-secondary); text-decoration: none; font-size: 14px; }
      .hub-tabs a:hover, .hub-tabs a.selected { background: var(--accent-muted); color: var(--accent); }
      .hub-tabs a.selected { font-weight: 600; }
      .hub-tabs a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
      @media (max-width: 720px) { .hub { padding: 20px 16px; } }
      .hub-panel {
        flex: 1 1 auto;
        min-height: 0;
      }
    `,
  ],
})
export class HubShellComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly auth = inject(AuthService);
  readonly title: string = this.route.snapshot.data['title'] ?? '';
  readonly description: string = this.title === 'Learning'
    ? 'Your programme, courses, and progress. One continuing development record.'
    : 'Build the content and environments behind a purposeful training experience.';
  readonly tabs: HubTab[] = this.route.snapshot.data['tabs'] ?? [];

  readonly visibleTabs = computed(() =>
    this.tabs.filter(tab => !tab.instructorOnly || this.auth.isInstructor()),
  );
}
