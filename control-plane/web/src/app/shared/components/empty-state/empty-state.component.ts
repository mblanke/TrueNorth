import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';

/**
 * The one empty-state look: icon, title, optional supporting line, and a
 * projected slot for an action button. Pages used to hand-roll a bare
 * paragraph (or nothing) when a list came back empty.
 */
@Component({
  selector: 'tn-empty-state',
  standalone: true,
  imports: [MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="wrap">
      <mat-icon aria-hidden="true">{{ icon }}</mat-icon>
      <div class="copy"><p class="title">{{ title }}</p>
      @if (message) {
        <p class="message">{{ message }}</p>
      }
      </div>
      <div class="actions">
      <ng-content />
      </div>
    </div>
  `,
  styles: [
    `
      :host { display: block; }
      .wrap {
        display: grid;
        grid-template-columns: 36px minmax(0, 1fr) auto;
        align-items: center;
        gap: 16px;
        padding: 24px;
        border: 1px solid var(--border);
        border-radius: 8px;
        background: var(--bg-card);
      }
      mat-icon {
        font-size: 22px;
        width: 22px;
        height: 22px;
        color: var(--accent);
        padding: 7px;
        border-radius: 6px;
        background: var(--accent-muted);
      }
      .title { margin: 0; font-weight: 600; color: var(--text-primary); }
      .message { margin: 6px 0 0; font-size: 13px; line-height: 1.5; color: var(--text-secondary); max-width: 520px; }
      .actions:empty { display: none; }
      @media (max-width: 720px) { .wrap { grid-template-columns: 36px minmax(0, 1fr); padding: 20px; } .actions { grid-column: 2; } }
    `,
  ],
})
export class EmptyStateComponent {
  @Input() icon = 'inbox';
  @Input({ required: true }) title = '';
  @Input() message?: string;
}
