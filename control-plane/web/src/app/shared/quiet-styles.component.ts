import { ChangeDetectionStrategy, Component, ViewEncapsulation } from '@angular/core';

/**
 * Carries `theme/quiet.scss` — the flat, calm look for pages marked `.tn-quiet` —
 * without putting it in the initial bundle.
 *
 * It renders nothing. With encapsulation off, Angular adds the stylesheet to the
 * document the first time this component is created, and leaves it there. The hub
 * shell renders it, so the styles arrive with the (lazy) hub pages that use them.
 */
@Component({
  selector: 'tn-quiet-styles',
  standalone: true,
  template: '',
  styleUrls: ['../../theme/quiet.scss'],
  encapsulation: ViewEncapsulation.None,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QuietStylesComponent {}
