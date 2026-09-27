import { Injectable, signal, computed } from '@angular/core';

export interface TourStep {
  /** Matches a `data-tour="..."` attribute somewhere in the DOM. */
  anchor: string;
  title: string;
  body: string;
  /** Preferred placement; falls back automatically if it would leave the viewport. */
  placement?: 'top' | 'bottom' | 'left' | 'right';
}

const SEEN_KEY = 'tn_tour_seen';

/**
 * A small guided tour, driven by `data-tour` attributes.
 *
 * Deliberately not a third-party tour library. The whole requirement is
 * "highlight four things once", and the existing MotionService/gsap already
 * handles the animation — a dependency for this would be more code to audit
 * than the feature contains.
 *
 * Anchors are looked up at step time rather than registered up front, so a
 * screen can add `data-tour` without knowing the tour exists.
 */
@Injectable({ providedIn: 'root' })
export class TourService {
  private stepsSignal = signal<TourStep[]>([]);
  private indexSignal = signal(0);
  private activeSignal = signal(false);

  readonly steps = this.stepsSignal.asReadonly();
  readonly index = this.indexSignal.asReadonly();
  readonly active = this.activeSignal.asReadonly();
  readonly current = computed(() => this.stepsSignal()[this.indexSignal()] ?? null);
  readonly isLast = computed(() => this.indexSignal() >= this.stepsSignal().length - 1);

  /** Start a tour. Steps whose anchor is not in the DOM are skipped, not fatal. */
  start(steps: TourStep[]): void {
    const present = steps.filter((s) => this.element(s.anchor) !== null);
    if (!present.length) {
      return;
    }
    this.stepsSignal.set(present);
    this.indexSignal.set(0);
    this.activeSignal.set(true);
    this.scrollTo(present[0]);
  }

  next(): void {
    if (this.isLast()) {
      this.finish();
      return;
    }
    this.indexSignal.update((i) => i + 1);
    const step = this.current();
    if (step) {
      this.scrollTo(step);
    }
  }

  previous(): void {
    if (this.indexSignal() === 0) {
      return;
    }
    this.indexSignal.update((i) => i - 1);
    const step = this.current();
    if (step) {
      this.scrollTo(step);
    }
  }

  /** Dismiss. Recorded so it does not reappear on every navigation. */
  finish(): void {
    this.activeSignal.set(false);
    this.stepsSignal.set([]);
    this.indexSignal.set(0);
    try {
      localStorage.setItem(SEEN_KEY, '1');
    } catch {
      // Private browsing or blocked site data — the tour simply shows again.
    }
  }

  hasSeen(): boolean {
    try {
      return localStorage.getItem(SEEN_KEY) === '1';
    } catch {
      return false;
    }
  }

  element(anchor: string): HTMLElement | null {
    return document.querySelector<HTMLElement>(`[data-tour="${anchor}"]`);
  }

  private scrollTo(step: TourStep): void {
    const el = this.element(step.anchor);
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}
