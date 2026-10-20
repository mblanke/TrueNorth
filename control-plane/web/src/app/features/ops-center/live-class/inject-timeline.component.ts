import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import type { InjectRecord } from '@core/models';
import { tplus } from './live-class';

interface Mark {
  rec: InjectRecord;
  pct: number;
  label: string;
}

/**
 * The injects of this run across the exercise clock (GET /exercises/{id}/injects), from the
 * start to now. Only what was recorded: fired, failed or skipped. A failed inject is the
 * exercise's fault and is shown here only, never against a Student. Planned injects that have
 * not fired yet are not shown (the scenario timeline is not read here).
 */
@Component({
  selector: 'tn-inject-timeline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="tl" aria-labelledby="tl-title">
      <div class="tl-head">
        <h2 id="tl-title">Injects</h2>
        <div class="legend" aria-label="Inject counts">
          <span><i class="dot fired"></i>Fired {{ counts().fired }}</span>
          <span><i class="dot failed"></i>Failed {{ counts().failed }}</span>
          <span><i class="dot skipped"></i>Skipped {{ counts().skipped }}</span>
        </div>
      </div>
      @if (!startedAt()) {
        <p class="muted">The exercise has not started; injects appear here as they fire.</p>
      } @else {
        <div class="track">
          <div class="axis"></div>
          @for (m of marks(); track m.rec.id) {
            <button type="button" [class]="'inj ' + m.rec.status" [style.left.%]="m.pct"
                    [attr.aria-label]="m.label" [attr.aria-pressed]="selected() === m.rec.id"
                    [title]="m.label" (click)="selected.set(m.rec.id)" (focus)="selected.set(m.rec.id)"></button>
          }
          <div class="now" [style.left.%]="100"><span>Now {{ nowLabel() }}</span></div>
        </div>
        <p class="detail" aria-live="polite">
          @if (selectedMark(); as m) {
            {{ m.label }}@if (m.rec.status === 'failed') { (an exercise-level failure, not counted against any Student)}
          } @else if (marks().length) {
            Last: {{ marks()[marks().length - 1].label }}
          } @else {
            No injects recorded in this run yet.
          }
        </p>
      }
    </section>
  `,
  styles: [`
    .tl { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md, 8px); padding: 12px 16px 8px; margin-bottom: 16px; }
    .tl-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
    h2 { font-size: 14px; margin: 0; }
    .legend { display: flex; gap: 14px; font-size: 12px; color: var(--text-secondary); }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    .dot.fired, .inj.fired { background: var(--text-secondary); }
    .dot.failed, .inj.failed { background: var(--warning); border-radius: 2px; }
    .dot.skipped, .inj.skipped { background: transparent; border: 2px solid var(--border-strong, var(--border)); }
    .track { position: relative; height: 44px; margin: 10px 8px 0 4px; }
    .axis { position: absolute; left: 0; right: 0; top: 20px; height: 3px; border-radius: 2px; background: var(--border); }
    .inj { position: absolute; top: 14px; width: 14px; height: 14px; margin-left: -7px; border-radius: 50%; border: 0; padding: 0; cursor: pointer; }
    .inj.failed { border-radius: 3px; }
    .inj[aria-pressed="true"] { outline: 2px solid var(--accent); outline-offset: 2px; }
    .now { position: absolute; top: 6px; bottom: 0; width: 2px; margin-left: -2px; background: var(--accent); }
    .now span { position: absolute; top: 28px; right: 6px; font-size: 11px; font-weight: 600; color: var(--accent); white-space: nowrap; }
    .detail { font-size: 12px; color: var(--text-secondary); min-height: 18px; margin: 4px 0 0; }
    .muted { font-size: 12px; color: var(--text-muted); margin: 6px 0; }
    @media (prefers-reduced-motion: no-preference) { .inj { transition: transform .12s; } .inj:hover, .inj:focus-visible { transform: scale(1.2); } }
  `],
})
export class InjectTimelineComponent {
  readonly injects = input<InjectRecord[]>([]);
  readonly startedAt = input<string | null | undefined>(null);
  /** The end of the axis: now, or when the exercise completed. */
  readonly endAt = input<string>(new Date().toISOString());

  readonly selected = signal<string | null>(null);

  readonly marks = computed<Mark[]>(() => {
    const start = this.startedAt();
    if (!start) return [];
    const t0 = new Date(start).getTime();
    const run = this.injects().filter(r => r.created_at && new Date(r.created_at).getTime() >= t0);
    const t1 = Math.max(new Date(this.endAt()).getTime(), ...run.map(r => new Date(r.created_at!).getTime()), t0 + 60000);
    return run.map(rec => {
      const at = new Date(rec.created_at!).getTime();
      const what = rec.action || (rec.source === 'instructor' ? 'Instructor inject' : `Inject ${rec.seq ?? ''}`.trim());
      const tech = rec.mitre_technique ? ` (${rec.mitre_technique})` : '';
      return {
        rec,
        pct: Math.min(100, Math.max(0, ((at - t0) / (t1 - t0)) * 100)),
        label: `${tplus(rec.created_at, start)} ${what}${tech}, ${rec.status}`,
      };
    });
  });

  readonly counts = computed(() => {
    const c = { fired: 0, failed: 0, skipped: 0 };
    for (const m of this.marks()) c[m.rec.status] += 1;
    return c;
  });

  readonly selectedMark = computed(() => this.marks().find(m => m.rec.id === this.selected()) ?? null);
  readonly nowLabel = computed(() => tplus(this.endAt(), this.startedAt()));
}
