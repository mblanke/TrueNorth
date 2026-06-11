import { AfterViewInit, Directive, ElementRef, Input, OnDestroy } from '@angular/core';

import { MotionService } from './motion.service';

/**
 * Staggers the entrance of a container's direct children.
 *
 * Usage: <div class="card-grid" tnEnterStagger> ... </div>
 * Children may carry the global `.tn-stagger-item` class to pre-hide
 * themselves (gated on prefers-reduced-motion in the global stylesheet).
 */
@Directive({
  selector: '[tnEnterStagger]',
  standalone: true,
})
export class EnterStaggerDirective implements AfterViewInit, OnDestroy {
  @Input() tnStaggerDelay = 0;
  @Input() tnStaggerY = 16;

  private tween?: gsap.core.Tween;

  constructor(
    private host: ElementRef<HTMLElement>,
    private motion: MotionService,
  ) {}

  ngAfterViewInit(): void {
    const children = Array.from(this.host.nativeElement.children);
    if (!children.length) {
      return;
    }
    children.forEach((c) => c.classList.remove('tn-stagger-item'));
    this.tween = this.motion.staggerIn(children, {
      delay: this.tnStaggerDelay,
      y: this.tnStaggerY,
    });
  }

  ngOnDestroy(): void {
    this.tween?.kill();
  }
}
