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
      <p class="title">{{ title }}</p>
      @if (message) {
        <p class="message">{{ message }}</p>
      }
      <ng-content />
    </div>
  `,
  styles: [
    `
      :host { display: block; }
      .wrap {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        padding: 48px 20px;
        text-align: center;
      }
      mat-icon {
        font-size: 48px;
        width: 48px;
        height: 48px;
        color: var(--text-muted);
        opacity: 0.5;
        margin-bottom: 8px;
      }
      .title { margin: 0; font-weight: 600; color: var(--text-primary); }
      .message { margin: 0; font-size: 0.86rem; color: var(--text-secondary); max-width: 420px; }
      .wrap > :last-child:not(.title):not(.message) { margin-top: 12px; }
    `,
  ],
})
export class EmptyStateComponent {
  @Input() icon = 'inbox';
  @Input({ required: true }) title = '';
  @Input() message?: string;
}
