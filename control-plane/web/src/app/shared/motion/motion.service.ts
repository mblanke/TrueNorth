import { Injectable, NgZone, signal, Signal } from '@angular/core';
import gsap from 'gsap';

export interface StaggerOptions {
  delay?: number;
  y?: number;
  duration?: number;
  stagger?: number;
}

export interface CountUpOptions {
  duration?: number;
  format?: (n: number) => string;
}

/**
 * Central GSAP wrapper. Every tween is created outside Angular's zone so
 * animation frames never trigger change detection. When the user prefers
 * reduced motion all durations collapse to zero, so callers get the end
 * state instantly through the same code path.
 */
@Injectable({ providedIn: 'root' })
export class MotionService {
  private readonly reducedMotionSignal = signal(false);
  readonly reducedMotion: Signal<boolean> = this.reducedMotionSignal.asReadonly();

  constructor(private zone: NgZone) {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.reducedMotionSignal.set(mq.matches);
    mq.addEventListener('change', (e) => this.reducedMotionSignal.set(e.matches));
  }

  staggerIn(els: Element[] | NodeListOf<Element>, opts: StaggerOptions = {}): gsap.core.Tween {
    const targets = Array.from(els);
    const reduced = this.reducedMotionSignal();
    return this.zone.runOutsideAngular(() =>
      gsap.fromTo(
        targets,
        { autoAlpha: 0, y: reduced ? 0 : opts.y ?? 16 },
        {
          autoAlpha: 1,
          y: 0,
          duration: reduced ? 0 : opts.duration ?? 0.45,
          stagger: reduced ? 0 : opts.stagger ?? 0.06,
          delay: reduced ? 0 : opts.delay ?? 0,
          ease: 'power2.out',
          clearProps: 'all',
        },
      ),
    );
  }

  countUp(el: HTMLElement, to: number, opts: CountUpOptions = {}): gsap.core.Tween {
    const format = opts.format ?? ((n: number) => Math.round(n).toLocaleString());
    const proxy = { value: 0 };
    const reduced = this.reducedMotionSignal();
    return this.zone.runOutsideAngular(() =>
      gsap.to(proxy, {
        value: to,
        duration: reduced ? 0 : opts.duration ?? 1.2,
        ease: 'power2.out',
        onUpdate: () => {
          el.textContent = format(proxy.value);
        },
      }),
    );
  }

  pageEnter(el: HTMLElement): gsap.core.Tween {
    const reduced = this.reducedMotionSignal();
    return this.zone.runOutsideAngular(() =>
      gsap.fromTo(
        el,
        { autoAlpha: 0, y: reduced ? 0 : 12 },
        {
          autoAlpha: 1,
          y: 0,
          duration: reduced ? 0 : 0.32,
          ease: 'power2.out',
          // A lingering transform creates a containing block that breaks
          // position:fixed descendants and JointJS pointer math.
          clearProps: 'transform,opacity,visibility',
        },
      ),
    );
  }

  createTimeline(vars?: gsap.TimelineVars): gsap.core.Timeline {
    return this.zone.runOutsideAngular(() => gsap.timeline(vars));
  }

  runOutside<T>(fn: () => T): T {
    return this.zone.runOutsideAngular(fn);
  }
}
