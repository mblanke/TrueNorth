import { Directive, ElementRef, OnDestroy, OnInit } from '@angular/core';
import gsap from 'gsap';

import { MotionService } from './motion.service';

/**
 * Lifts the host slightly on hover. The accompanying `.is-lifted` class is
 * toggled so CSS owns the theme-reactive glow shadow. Listeners are bound
 * outside Angular's zone — hover never triggers change detection.
 */
@Directive({
  selector: '[tnHoverLift]',
  standalone: true,
})
export class HoverLiftDirective implements OnInit, OnDestroy {
  private enter = () => {
    this.host.nativeElement.classList.add('is-lifted');
    if (!this.motion.reducedMotion()) {
      gsap.to(this.host.nativeElement, { y: -3, duration: 0.25, ease: 'power2.out' });
    }
  };

  private leave = () => {
    this.host.nativeElement.classList.remove('is-lifted');
    if (!this.motion.reducedMotion()) {
      gsap.to(this.host.nativeElement, { y: 0, duration: 0.3, ease: 'power2.out', clearProps: 'transform' });
    }
  };

  constructor(
    private host: ElementRef<HTMLElement>,
    private motion: MotionService,
  ) {}

  ngOnInit(): void {
    this.motion.runOutside(() => {
      this.host.nativeElement.addEventListener('pointerenter', this.enter);
      this.host.nativeElement.addEventListener('pointerleave', this.leave);
    });
  }

  ngOnDestroy(): void {
    this.host.nativeElement.removeEventListener('pointerenter', this.enter);
    this.host.nativeElement.removeEventListener('pointerleave', this.leave);
    gsap.killTweensOf(this.host.nativeElement);
  }
}
