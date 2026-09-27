import {
  Component,
  ElementRef,
  HostListener,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { TourService } from './tour.service';

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

/**
 * Renders the active tour step: a dimmed backdrop with a hole cut over the
 * anchored element, and a callout beside it.
 *
 * The hole is an SVG mask rather than four positioned divs — with divs, the
 * edges never line up exactly at fractional device pixels and the highlight
 * looks smeared.
 */
@Component({
  selector: 'tn-tour-overlay',
  standalone: true,
  imports: [CommonModule, MatButtonModule, MatIconModule],
  template: `
    @if (tour.active() && box(); as b) {
      <div class="tour-root" role="dialog" aria-modal="true" [attr.aria-label]="step()?.title">
        <svg class="tour-scrim" (click)="tour.finish()">
          <defs>
            <mask id="tn-tour-mask">
              <rect width="100%" height="100%" fill="white" />
              <rect
                [attr.x]="b.left - 6"
                [attr.y]="b.top - 6"
                [attr.width]="b.width + 12"
                [attr.height]="b.height + 12"
                rx="8"
                fill="black" />
            </mask>
          </defs>
          <rect width="100%" height="100%" fill="rgba(0,0,0,0.55)" mask="url(#tn-tour-mask)" />
        </svg>

        <div
          class="tour-ring"
          [style.top.px]="b.top - 6"
          [style.left.px]="b.left - 6"
          [style.width.px]="b.width + 12"
          [style.height.px]="b.height + 12"></div>

        <div class="tour-card" [style.top.px]="cardTop()" [style.left.px]="cardLeft()">
          <div class="tour-head">
            <strong>{{ step()?.title }}</strong>
            <button mat-icon-button aria-label="Close tour" (click)="tour.finish()">
              <mat-icon>close</mat-icon>
            </button>
          </div>
          <p>{{ step()?.body }}</p>
          <div class="tour-foot">
            <span class="tour-count">{{ tour.index() + 1 }} of {{ tour.steps().length }}</span>
            <span class="tour-spacer"></span>
            @if (tour.index() > 0) {
              <button mat-button (click)="tour.previous()">Back</button>
            }
            <button mat-flat-button color="primary" (click)="tour.next()">
              {{ tour.isLast() ? 'Done' : 'Next' }}
            </button>
          </div>
        </div>
      </div>
    }
  `,
  styles: [
    `
      .tour-root {
        position: fixed;
        inset: 0;
        z-index: 2000;
      }
      .tour-scrim {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
      }
      .tour-ring {
        position: fixed;
        border-radius: 8px;
        border: 2px solid var(--tn-primary, #3f51b5);
        pointer-events: none;
        transition:
          top 0.2s ease,
          left 0.2s ease,
          width 0.2s ease,
          height 0.2s ease;
      }
      .tour-card {
        position: fixed;
        width: min(320px, calc(100vw - 2rem));
        background: var(--tn-surface, #fff);
        color: var(--tn-on-surface, inherit);
        border-radius: 10px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
        padding: 0.75rem 1rem 0.5rem;
        transition:
          top 0.2s ease,
          left 0.2s ease;
      }
      .tour-head {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }
      .tour-head strong {
        flex: 1;
      }
      .tour-card p {
        margin: 0.25rem 0 0.5rem;
        font-size: 0.9rem;
        opacity: 0.85;
      }
      .tour-foot {
        display: flex;
        align-items: center;
        gap: 0.25rem;
      }
      .tour-count {
        font-size: 0.75rem;
        opacity: 0.6;
      }
      .tour-spacer {
        flex: 1;
      }
      @media (prefers-reduced-motion: reduce) {
        .tour-ring,
        .tour-card {
          transition: none;
        }
      }
    `,
  ],
})
export class TourOverlayComponent implements OnInit, OnDestroy {
  readonly tour = inject(TourService);
  private readonly host = inject(ElementRef);

  private boxSignal = signal<Box | null>(null);
  readonly box = this.boxSignal.asReadonly();
  readonly step = computed(() => this.tour.current());

  private raf = 0;

  ngOnInit(): void {
    this.track();
  }

  ngOnDestroy(): void {
    cancelAnimationFrame(this.raf);
  }

  /**
   * Re-read the anchor position each frame while the tour is open.
   *
   * A resize/scroll listener is not enough: the highlighted element can move
   * because a lazy chunk finished loading or a panel expanded, with no event
   * either listener would see.
   */
  private track = (): void => {
    const step = this.tour.current();
    if (this.tour.active() && step) {
      const el = this.tour.element(step.anchor);
      if (el) {
        const r = el.getBoundingClientRect();
        const next = { top: r.top, left: r.left, width: r.width, height: r.height };
        const prev = this.boxSignal();
        if (
          !prev ||
          prev.top !== next.top ||
          prev.left !== next.left ||
          prev.width !== next.width ||
          prev.height !== next.height
        ) {
          this.boxSignal.set(next);
        }
      } else {
        // The anchor vanished (navigation, collapsed panel). Move on rather
        // than leaving a highlight over nothing.
        this.tour.next();
      }
    } else if (this.boxSignal() !== null) {
      this.boxSignal.set(null);
    }
    this.raf = requestAnimationFrame(this.track);
  };

  @HostListener('document:keydown.escape')
  onEscape(): void {
    if (this.tour.active()) {
      this.tour.finish();
    }
  }

  cardTop(): number {
    const b = this.boxSignal();
    if (!b) {
      return 0;
    }
    const below = b.top + b.height + 14;
    // Flip above when the callout would run off the bottom.
    return below + 190 > window.innerHeight ? Math.max(8, b.top - 190) : below;
  }

  cardLeft(): number {
    const b = this.boxSignal();
    if (!b) {
      return 0;
    }
    const width = Math.min(320, window.innerWidth - 32);
    return Math.max(16, Math.min(b.left, window.innerWidth - width - 16));
  }
}
