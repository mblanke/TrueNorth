import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/** Load bands: warning from 75 %, critical from 90 % (same thresholds as the bar gauges). */
export const LOAD_WARN = 75;
export const LOAD_CRIT = 90;

export type LoadBand = 'normal' | 'warning' | 'critical' | 'unknown';

export function loadBand(pct: number | null | undefined): LoadBand {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return 'unknown';
  if (pct >= LOAD_CRIT) return 'critical';
  if (pct >= LOAD_WARN) return 'warning';
  return 'normal';
}

/** used / total as a percent, or null when either is unreported or the total is not positive.
 *  Never 0 for "unknown": a missing figure is not an idle host. */
export function loadPct(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used === null || used === undefined || total === null || total === undefined || !(total > 0)) return null;
  return (used / total) * 100;
}

/**
 * A small semicircle "speedometer" for one load figure (CPU, memory, storage).
 * Presentational only: give it a percent (or null when the source did not report one).
 * Unknown renders as an empty grey arc labelled "not reported", never 0 %.
 */
@Component({
  selector: 'tn-load-gauge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[attr.role]': 'known() ? "meter" : "img"',
    '[attr.aria-valuenow]': 'known() ? rounded() : null',
    '[attr.aria-valuemin]': 'known() ? 0 : null',
    '[attr.aria-valuemax]': 'known() ? 100 : null',
    '[attr.aria-label]': 'ariaLabel()',
    '[class]': '"band-" + band()',
  },
  template: `
    <svg viewBox="0 0 120 70" aria-hidden="true" focusable="false">
      <path class="track" d="M 12 62 A 48 48 0 0 1 108 62" pathLength="100" />
      @if (known()) {
        <path class="fill" d="M 12 62 A 48 48 0 0 1 108 62" pathLength="100"
          [attr.stroke-dasharray]="clamped() + ' 100'" />
      }
      <text class="value" x="60" y="58" text-anchor="middle">{{ known() ? rounded() + '%' : '—' }}</text>
    </svg>
    <div class="caption">{{ label() }}</div>
    @if (!known()) {
      <div class="unknown">not reported</div>
    }
  `,
  styles: [`
    :host { display: inline-flex; flex-direction: column; align-items: center; width: 104px; --gauge-color: var(--success); }
    :host(.band-warning) { --gauge-color: var(--warning); }
    :host(.band-critical) { --gauge-color: var(--alert); }
    :host(.band-unknown) { --gauge-color: var(--text-muted); }
    svg { width: 100%; height: auto; display: block; overflow: visible; }
    path { fill: none; stroke-width: 10; stroke-linecap: round; }
    .track { stroke: var(--border); }
    .fill { stroke: var(--gauge-color); transition: stroke-dasharray 0.6s ease; }
    .value { font-size: 18px; font-weight: 600; fill: var(--text-primary); }
    :host(.band-unknown) .value { fill: var(--text-muted); }
    .caption { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
    .unknown { font-size: 11px; color: var(--text-muted); font-style: italic; }
    @media (prefers-reduced-motion: reduce) { .fill { transition: none; } }
  `],
})
export class LoadGaugeComponent {
  /** Caption, e.g. "CPU". */
  readonly label = input.required<string>();
  /** Percent 0-100; null/undefined when not reported. */
  readonly value = input<number | null | undefined>(null);

  readonly known = computed(() => loadBand(this.value()) !== 'unknown');
  readonly clamped = computed(() => Math.min(100, Math.max(0, this.value() ?? 0)));
  readonly rounded = computed(() => Math.round(this.clamped()));
  /** Banded on the figure shown, so "75%" is never drawn as normal. */
  readonly band = computed(() => loadBand(this.known() ? this.rounded() : null));
  readonly ariaLabel = computed(() =>
    this.known() ? `${this.label()} load ${this.rounded()} percent` : `${this.label()} load not reported`);
}
