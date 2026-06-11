import { Directive, ElementRef, Input, OnChanges, OnDestroy, SimpleChanges } from '@angular/core';

import { MotionService } from './motion.service';

/**
 * Animates the host element's text from its current value to the input
 * value. Re-animates whenever the bound value changes, so it fires
 * naturally when API data lands in a signal.
 *
 * Usage: <span [tnCountUp]="rangeCount()"></span>
 */
@Directive({
  selector: '[tnCountUp]',
  standalone: true,
})
export class CountUpDirective implements OnChanges, OnDestroy {
  @Input({ required: true }) tnCountUp: number | null | undefined;
  @Input() tnCountUpFormat?: (n: number) => string;

  private tween?: gsap.core.Tween;

  constructor(
    private host: ElementRef<HTMLElement>,
    private motion: MotionService,
  ) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (!('tnCountUp' in changes)) {
      return;
    }
    const target = this.tnCountUp;
    if (target === null || target === undefined || isNaN(target)) {
      this.host.nativeElement.textContent = '—';
      return;
    }
    this.tween?.kill();
    this.tween = this.motion.countUp(this.host.nativeElement, target, {
      format: this.tnCountUpFormat,
    });
  }

  ngOnDestroy(): void {
    this.tween?.kill();
  }
}
