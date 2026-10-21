import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { RangeCredentialsApiService, TemplateIssue } from '@core/services/range-credentials-api.service';

/**
 * Templates whose typed-in account passwords ADR 0013 §3 no longer accepts (not on a
 * `scenario: true` account, or on a staff login): ranges built from them cannot be
 * provisioned, scheduled bookings included, until the template is fixed. Shown to whoever
 * the API answers (template:update); anyone else gets 403 and sees nothing.
 */
@Component({
  selector: 'tn-template-issues-banner',
  imports: [MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (issues().length) {
      <section class="banner" role="alert" aria-label="Templates that cannot be provisioned">
        <h3><mat-icon>warning</mat-icon> {{ issues().length }} template(s) cannot be provisioned until fixed</h3>
        <p>
          Since ADR 0013 a typed-in account password is allowed only on an account marked
          <code>scenario: true</code> (its weak password is the lesson), never on a staff login.
          Mark the account, or use <code>generate: true</code>.
        </p>
        <ul>
          @for (i of issues(); track i.template_id) {
            <li>
              <b>{{ i.name }}</b> {{ i.version }}
              @if (i.upcoming_bookings) { <span class="soon">· {{ i.upcoming_bookings }} upcoming booking(s) will fail</span> }
              <ul class="problems">
                @for (p of i.problems; track p) { <li>{{ p }}</li> }
              </ul>
            </li>
          }
        </ul>
      </section>
    }
  `,
  styles: [`
    .banner { border: 1px solid var(--warning, #f59e0b); border-radius: 8px; padding: 8px 16px; margin-bottom: 16px; }
    h3 { display: flex; align-items: center; gap: 6px; margin: 4px 0; font-size: 15px; }
    p, li { font-size: 13px; }
    .soon { color: var(--severity-high, #dc2626); }
    .problems { color: var(--text-secondary); }
  `],
})
export class TemplateIssuesBannerComponent implements OnInit {
  private api = inject(RangeCredentialsApiService);
  readonly issues = signal<TemplateIssue[]>([]);

  ngOnInit(): void {
    this.api.templateIssues().subscribe({
      next: list => this.issues.set(list),
      error: () => this.issues.set([]),  // 403 for roles that cannot fix templates: nothing to show
    });
  }
}
