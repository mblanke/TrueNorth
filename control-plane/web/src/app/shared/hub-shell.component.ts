import { Component, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  ActivatedRoute,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from '@angular/router';
import { MatTabsModule } from '@angular/material/tabs';
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
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterLink, RouterLinkActive, MatTabsModule, QuietStylesComponent],
  template: `
    <tn-quiet-styles />
    <section class="hub">
      @if (title) {
        <h1 class="hub-title">{{ title }}</h1>
      }
      <nav mat-tab-nav-bar [tabPanel]="tabPanel" class="hub-tabs">
        @for (tab of visibleTabs(); track tab.path) {
          <a
            mat-tab-link
            [routerLink]="tab.path"
            routerLinkActive
            #rla="routerLinkActive"
            [active]="rla.isActive"
          >
            {{ tab.label }}
          </a>
        }
      </nav>
      <mat-tab-nav-panel #tabPanel class="hub-panel">
        <router-outlet />
      </mat-tab-nav-panel>
    </section>
  `,
  styles: [
    `
      .hub {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 0;
      }
      .hub-title {
        margin: 0 0 0.5rem;
        font-size: 1.25rem;
        font-weight: 600;
      }
      .hub-tabs {
        margin-bottom: 1rem;
      }
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
  readonly tabs: HubTab[] = this.route.snapshot.data['tabs'] ?? [];

  readonly visibleTabs = computed(() =>
    this.tabs.filter(tab => !tab.instructorOnly || this.auth.isInstructor()),
  );
}
