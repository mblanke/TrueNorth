import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatTooltipModule } from '@angular/material/tooltip';
import {
  CurriculumMap,
  DPStage,
  NodeState,
  QualNode,
} from '@core/services/curriculum-map.service';
import { QualificationDetailComponent, undeliveredObjectives } from './qualification-detail.component';

const STATE_LABEL: Record<NodeState, string> = {
  locked: 'Locked',
  available: 'Available',
  in_progress: 'In progress',
  complete: 'Complete',
};

/** The shared status-chip look for each stage state. */
const STATE_CHIP: Record<NodeState, string> = {
  locked: 'disabled',
  available: 'pending',
  in_progress: 'active',
  complete: 'completed',
};

/** One row of the path: a qualification, or a period nobody has written a QSP for. */
export type PathRow =
  | { kind: 'qual'; key: string; stage: DPStage; node: QualNode; trackLabel: string | null }
  | { kind: 'planned'; key: string; stage: DPStage };

/**
 * The developmental path as a vertical list: one quiet row per qualification, in
 * period order. A row shows only where it sits, what it is, and its state; opening it
 * shows the programme and objectives (see QualificationDetailComponent), which fold
 * again. The learner's current stage opens on load; everything else starts closed.
 *
 * Replaces the grid career map. Prerequisites were drawn there as arrows; here they
 * are the order of the list, which for a rank ladder is the same information.
 */
@Component({
  selector: 'tn-career-path-list',
  standalone: true,
  imports: [CommonModule, MatExpansionModule, MatTooltipModule, QualificationDetailComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (overall(); as o) {
      <div class="overall" aria-live="polite">
        <span class="tn-muted tn-small">
          {{ o.completed }} of {{ o.total }} objectives complete
        </span>
        <span
          class="bar"
          role="progressbar"
          [attr.aria-valuenow]="o.pct"
          aria-valuemin="0"
          aria-valuemax="100"
          [attr.aria-label]="o.pct + '% of the developmental path complete'"
        ><span class="fill" [style.width.%]="o.pct"></span></span>
        <span class="tn-small pct">{{ o.pct }}%</span>
      </div>
    }

    <mat-accordion class="path" displayMode="flat">
      @for (row of rows(); track row.key) {
        @if (row.kind === 'qual') {
          <mat-expansion-panel
            class="stage"
            [attr.data-qual]="row.node.qsp_code"
            [class.here]="row.node.qsp_code === currentQsp()"
            [expanded]="row.node.qsp_code === open()"
            (opened)="toggle(row.node.qsp_code, true)"
            (closed)="toggle(row.node.qsp_code, false)"
          >
            <mat-expansion-panel-header>
              <mat-panel-title>
                <span class="kicker tn-small">
                  DP {{ row.stage.dp_order }}@if (row.stage.rank_level) { · {{ row.stage.rank_level }} }
                </span>
                <!-- QSP codes stay off the face (placeholder codes like TEMP67 must not
                     read as real designations); they remain in the tooltip. -->
                <span class="title" [matTooltip]="row.node.qsp_code">{{ row.node.title }}</span>
                @if (row.trackLabel) {
                  <span class="track tn-small">{{ row.trackLabel }}</span>
                }
              </mat-panel-title>
              <mat-panel-description class="tn-small">
                @if (row.node.qsp_code === currentQsp()) {
                  <span class="you">you are here</span>
                }
                <!-- A gap has to stay visible as a gap, even with the row closed. -->
                @if (gapCount(row.node); as gaps) {
                  <span
                    class="tn-gap-count"
                    [matTooltip]="gaps + ' objectives have no course that delivers them yet'"
                  >{{ gaps }} not yet delivered</span>
                }
                <span class="progress">{{ row.node.progress.completed }}/{{ row.node.po_count }}</span>
                <span class="status-chip" [ngClass]="stateChip(row.node)">{{ stateLabel(row.node) }}</span>
              </mat-panel-description>
            </mat-expansion-panel-header>

            <ng-template matExpansionPanelContent>
              <tn-qualification-detail [qualification]="row.node" [currentPoCode]="currentPoCode" />
            </ng-template>
          </mat-expansion-panel>
        } @else {
          <!-- A period nobody has written a QSP for yet: shown so the ladder has no
               silent holes, but there is nothing to open. -->
          <div class="stage planned" [attr.data-planned]="row.stage.dp_order">
            <span class="kicker tn-small">
              DP {{ row.stage.dp_order }}@if (row.stage.rank_level) { · {{ row.stage.rank_level }} }
            </span>
            <span class="title">{{ row.stage.label || 'Not yet defined' }}</span>
            <span class="tn-small planned-note">planned</span>
          </div>
        }
      }
    </mat-accordion>
  `,
  styles: [
    `
      :host { display: block; }

      .overall {
        display: flex;
        align-items: center;
        gap: 12px;
        margin: 0 0 14px;
      }
      .bar {
        flex: 0 1 220px;
        height: 6px;
        border-radius: 999px;
        background: var(--border);
        overflow: hidden;
      }
      .fill {
        display: block;
        height: 100%;
        background: var(--accent);
        border-radius: inherit;
      }
      .pct { color: var(--text-secondary); font-variant-numeric: tabular-nums; }

      .kicker {
        flex: 0 0 auto;
        min-width: 7.5rem;
        color: var(--text-muted);
        font-weight: 500;
      }
      .title {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .track {
        flex: 0 0 auto;
        color: var(--text-muted);
        font-weight: 400;
      }
      .you { color: var(--accent); font-weight: 600; }
      .progress { font-variant-numeric: tabular-nums; }

      /* The current stage: one quiet accent line, no glow, no pulse. */
      .stage.here { box-shadow: inset 3px 0 0 var(--accent) !important; }

      /* Not yet written: set apart from the real stages, dashed, nothing to open. */
      .planned {
        display: flex;
        align-items: baseline;
        gap: 10px;
        min-height: 48px;
        margin-top: 8px;
        padding: 12px 16px;
        border: 1px dashed var(--border);
        border-radius: var(--radius-md);
        color: var(--text-muted);
      }
      .planned-note { margin-left: auto; font-style: italic; }

      @media (max-width: 640px) {
        .kicker { min-width: 0; }
        .track { display: none; }
      }
    `,
  ],
})
export class CareerPathListComponent {
  @Input({ required: true }) set map(value: CurriculumMap | null) {
    this.data.set(value);
  }

  /** The open qualification (held in the URL by the page as `?qual=`). */
  @Input() set selected(value: string | null) {
    this.open.set(value);
  }

  /** Highlights the objective the learner is on, inside the open stage. */
  @Input() currentPoCode: string | null = null;

  /** Emits the newly opened qualification, or null when the open one is closed. */
  @Output() readonly selectedChange = new EventEmitter<string | null>();

  protected readonly data = signal<CurriculumMap | null>(null);
  protected readonly open = signal<string | null>(null);

  protected readonly currentQsp = computed(() => this.data()?.learner?.current_qsp_code ?? null);

  /**
   * Period order, then the rank ladder before specialty streams (track order as the
   * API gives it). Planned periods appear once, and only where the ladder has no
   * qualification — a specialty stream has no claim on a DP nobody has written.
   */
  protected readonly rows = computed<PathRow[]>(() => {
    const map = this.data();
    if (!map) return [];
    const trackOrder = new Map(map.tracks.map((t, i) => [t.key, i]));
    const trackLabel = new Map(map.tracks.map(t => [t.key, t.kind === 'specialty' ? t.label : null]));
    const rows: PathRow[] = [];
    for (const stage of [...map.stages].sort((a, b) => a.dp_order - b.dp_order)) {
      const nodes = map.nodes
        .filter(n => n.dp_order === stage.dp_order)
        .sort((a, b) => (trackOrder.get(a.track_key) ?? 99) - (trackOrder.get(b.track_key) ?? 99));
      for (const node of nodes) {
        rows.push({
          kind: 'qual',
          key: node.qsp_code,
          stage,
          node,
          trackLabel: trackLabel.get(node.track_key) ?? null,
        });
      }
      if (!nodes.length && stage.planned) {
        rows.push({ kind: 'planned', key: `planned:${stage.dp_order}`, stage });
      }
    }
    return rows;
  });

  /** Progress across the whole path, for the one summary line at the top. */
  protected readonly overall = computed(() => {
    const nodes = this.data()?.nodes ?? [];
    const total = nodes.reduce((s, n) => s + (n.progress?.total || n.po_count || 0), 0);
    if (!total) return null;
    const completed = nodes.reduce((s, n) => s + (n.progress?.completed || 0), 0);
    return { completed, total, pct: Math.round((completed / total) * 100) };
  });

  protected toggle(code: string, opened: boolean): void {
    if (opened) {
      if (this.open() !== code) {
        this.open.set(code);
        this.selectedChange.emit(code);
      }
    } else if (this.open() === code) {
      this.open.set(null);
      this.selectedChange.emit(null);
    }
  }

  protected gapCount(node: QualNode): number {
    return undeliveredObjectives(node).length;
  }

  protected stateLabel(node: QualNode): string {
    return STATE_LABEL[node.state] ?? node.state;
  }

  protected stateChip(node: QualNode): string {
    return STATE_CHIP[node.state] ?? 'pending';
  }
}
