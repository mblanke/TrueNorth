import { AfterViewInit, Directive, ElementRef, Input, OnDestroy } from '@angular/core';

import { MotionService } from './motion.service';

/**
 * Staggers the entrance of a container's direct children.
 *
 * Usage: <div class="card-grid" tnEnterStagger> ... </div>
 * Children may carry the global `.tn-stagger-item` class to pre-hide
 * themselves (gated on prefers-reduced-motion in the global stylesheet).
 *
 * Children that arrive *after* first render are animated too. Most lists here come
 * from the API, so at ngAfterViewInit the container is usually empty; this used to
 * return early, leaving every later `.tn-stagger-item` at opacity 0 for good —
 * scenarios, templates, detection rules and more were loaded but invisible.
 */
@Directive({
  selector: '[tnEnterStagger]',
  standalone: true,
})
export class EnterStaggerDirective implements AfterViewInit, OnDestroy {
  @Input() tnStaggerDelay = 0;
  @Input() tnStaggerY = 16;

  private readonly tweens: gsap.core.Tween[] = [];
  private observer?: MutationObserver;

  constructor(
    private host: ElementRef<HTMLElement>,
    private motion: MotionService,
  ) {}

  ngAfterViewInit(): void {
    this.reveal(Array.from(this.host.nativeElement.children), this.tnStaggerDelay);
    if (typeof MutationObserver === 'undefined') return;
    this.observer = new MutationObserver(records => {
      const added = records
        .flatMap(r => Array.from(r.addedNodes))
        .filter((n): n is Element => n.nodeType === Node.ELEMENT_NODE);
      this.reveal(added, 0);
    });
    this.observer.observe(this.host.nativeElement, { childList: true });
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
    this.tweens.forEach(t => t.kill());
  }

  private reveal(els: Element[], delay: number): void {
    if (!els.length) return;
    els.forEach(c => c.classList.remove('tn-stagger-item'));
    this.tweens.push(this.motion.staggerIn(els, { delay, y: this.tnStaggerY }));
  }
}
