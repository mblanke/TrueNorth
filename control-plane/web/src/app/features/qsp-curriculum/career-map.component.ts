import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  NgZone,
  OnDestroy,
  Output,
  QueryList,
  ViewChild,
  ViewChildren,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import {
  CurriculumMap,
  DPStage,
  DPTrack,
  NodeCourse,
  NodeState,
  PathEdge,
  QualNode,
  TermGroup,
  groupByTerm,
} from '@core/services/curriculum-map.service';

/** A node placed into its lane/column cell, or a ghost placeholder for a future DP. */
interface Cell {
  key: string;
  node: QualNode | null;
  stage: DPStage;
  trackKey: string;
  /** Index into the flat, keyboard-navigable cell list. */
  index: number;
  /**
   * Whether anything is drawn here. Empty cells still have to exist so the grid
   * keeps its columns aligned, but they are hidden once the map stacks vertically.
   */
  filled: boolean;
}

interface Lane {
  track: DPTrack;
  cells: Cell[];
}

/** A connector resolved to real pixel geometry. */
interface DrawnEdge {
  path: string;
  kind: PathEdge['kind'];
  state: NodeState | 'planned';
  /** Logical endpoints, kept so the chain highlight can test membership. */
  from: string;
  to: string;
}

const RING_R = 15;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_R;

const STATE_LABEL: Record<NodeState, string> = {
  locked: 'Locked',
  available: 'Available',
  in_progress: 'In progress',
  complete: 'Complete',
};

/**
 * The developmental career map: track lanes down, DP columns across, with the
 * prerequisite edges drawn between them.
 *
 * Layout is a plain CSS grid — the edges are the only thing that needs real
 * geometry, so a single SVG overlay is measured off the laid-out cells and
 * recomputed on resize. That keeps the whole thing dependency-free and lets the
 * grid handle reflow, which a graph library would fight.
 */
@Component({
  selector: 'tn-career-map',
  standalone: true,
  imports: [CommonModule, RouterLink, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="map-scroll">
      <div
        class="map"
        #grid
        [style.--stage-count]="stages().length"
        [class.chain-active]="!!chainSource()"
      >
        <!-- Column headers -->
        <div class="corner" aria-hidden="true"></div>
        @for (stage of stages(); track stage.dp_order) {
          <div class="stage-head" [class.planned]="stage.planned">
            <span class="dp">DP {{ stage.dp_order }}</span>
            @if (stage.rank_level) {
              <span class="rank">{{ stage.rank_level }}</span>
            }
            <span class="stage-label">{{ stage.planned ? 'Not yet defined' : stage.label }}</span>
          </div>
        }

        <!-- Edges sit under the cards but above the grid background -->
        <svg class="edges" [attr.viewBox]="viewBox()" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <!-- One arrowhead per state: Chrome has no context-stroke in markers, so a
                 single marker cannot inherit the edge's stroke. Marker ids are
                 document-global, which is safe while exactly one map renders per page. -->
            @for (s of edgeStates; track s) {
              <marker
                [attr.id]="'edge-arrow-' + s"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="8"
                markerHeight="8"
                markerUnits="userSpaceOnUse"
                orient="auto"
              >
                <path d="M 0 0 L 8 4 L 0 8 z" class="edge-arrow" [attr.data-state]="s" />
              </marker>
            }
          </defs>
          @for (edge of drawnEdges(); track $index) {
            <path
              [attr.d]="edge.path"
              class="edge"
              [class.branch]="edge.kind === 'branch'"
              [class.planned]="edge.kind === 'planned'"
              [class.on-chain]="chainNodes().has(edge.from) && chainNodes().has(edge.to)"
              [attr.data-state]="edge.state"
              [attr.marker-end]="'url(#edge-arrow-' + edge.state + ')'"
            />
          }
        </svg>

        <!-- Lanes -->
        @for (lane of lanes(); track lane.track.key) {
          <div class="lane-head" [class.specialty]="lane.track.kind === 'specialty'">
            <span class="lane-name">{{ lane.track.label }}</span>
            @if (lane.track.kind === 'specialty') {
              <span class="lane-kind">specialty stream</span>
            }
          </div>

          @for (cell of lane.cells; track cell.key) {
            <div class="cell" role="gridcell" [class.is-empty]="!cell.filled">
              @if (cell.node; as node) {
                <!-- The bubble is a container, not a control: the courses inside it are
                     links, and an anchor inside a button is invalid and unreachable by
                     keyboard. The selectable card is the button within. The data-node
                     hook sits here so edges anchor to the whole bubble, courses
                     included. -->
                <div
                  class="node"
                  [class]="'state-' + node.state"
                  [class.selected]="node.qsp_code === selected"
                  [class.here]="node.qsp_code === currentQsp()"
                  [class.on-chain]="chainNodes().has(node.qsp_code)"
                  [attr.data-node]="node.qsp_code"
                  (pointerenter)="chainSource.set(node.qsp_code)"
                  (pointerleave)="chainSource.set(null)"
                >
                <button
                  type="button"
                  class="node-main"
                  [attr.tabindex]="cell.index === focusIndex() ? 0 : -1"
                  [attr.aria-current]="node.qsp_code === currentQsp() ? 'step' : null"
                  [attr.aria-pressed]="node.qsp_code === selected"
                  [attr.aria-label]="ariaLabel(node)"
                  (click)="pick(node, cell.index)"
                  (focus)="focusIndex.set(cell.index); chainSource.set(node.qsp_code)"
                  (blur)="chainSource.set(null)"
                  #cellButton
                >
                  <span class="node-eyebrow">
                    DP {{ node.dp_order }}
                    @if (node.rank_level) { · {{ node.rank_level }} }
                  </span>

                  <span class="node-body">
                    <svg class="ring" viewBox="0 0 36 36" aria-hidden="true">
                      <circle class="ring-track" cx="18" cy="18" [attr.r]="ringR" />
                      <circle
                        class="ring-fill"
                        cx="18"
                        cy="18"
                        [attr.r]="ringR"
                        [attr.stroke-dasharray]="ringCircumference"
                        [attr.stroke-dashoffset]="ringOffset(node)"
                      />
                      <text class="ring-text" x="18" y="18">
                        {{ node.progress.pct }}<tspan class="ring-pct">%</tspan>
                      </text>
                    </svg>

                    <span class="node-text">
                      <!-- The QSP code is not shown: ALJQ is internal shorthand and
                           TEMP67/TEMP64 are placeholder identifiers for qualifications
                           whose real codes are not yet issued. Showing a placeholder as
                           if it were a designation invites it being read as one. The
                           code stays in the accessible label, where it identifies the
                           node without asserting anything on screen. -->
                      <span class="node-title" [matTooltip]="node.title">{{ node.title }}</span>
                    </span>
                  </span>

                  <span class="node-meta">
                    <span class="meta-item">
                      {{ node.progress.completed }}/{{ node.po_count }} objectives
                    </span>
                    @if (node.course_count) {
                      <span
                        class="meta-item"
                        [matTooltip]="programmeTooltip(node)"
                      >
                        <mat-icon>menu_book</mat-icon>{{ node.course_count }} courses
                        @if (node.course_hours) { · {{ fmtCourseHours(node) }} }
                      </span>
                    }
                    @if (node.gate_count) {
                      <span class="meta-item gate" matTooltip="Contains a gating assessment">
                        <mat-icon>flag</mat-icon>{{ node.gate_count }}
                      </span>
                    }
                    @if (node.total_minutes) {
                      <span
                        class="meta-item"
                        [class.partial]="isPartiallyTimed(node)"
                        [matTooltip]="durationTooltip(node)"
                      >
                        {{ fmtHours(node.total_minutes) }}{{ isPartiallyTimed(node) ? '+' : '' }}
                        assessed
                      </span>
                    } @else if (node.po_count) {
                      <span class="meta-item partial" matTooltip="No objective has a duration yet">
                        unscoped
                      </span>
                    }
                    @if (node.state === 'locked') {
                      <mat-icon class="state-icon">lock</mat-icon>
                    } @else if (node.state === 'complete') {
                      <mat-icon class="state-icon done">check_circle</mat-icon>
                    }
                  </span>

                  @if (node.qsp_code === currentQsp()) {
                    <span class="here-tag">You are here</span>
                  }
                </button>

                <!-- The programme taught in this period, as a track inside the bubble:
                     DP1 is three years of Algonquin terms, DP2 the RMC year. The card
                     used to report a course count and show none of it. -->
                @if (node.courses.length) {
                  <ol class="term-track" [attr.aria-label]="'Programme for ' + node.title">
                    @for (term of termsOf(node); track term.code) {
                      <li class="term">
                        <span class="term-head">
                          <span class="term-name">{{ term.short }}</span>
                          <span class="term-count">{{ term.courses.length }}</span>
                        </span>
                        <span class="term-courses">
                          @for (c of term.courses; track c.course_id) {
                            <a
                              class="c-chip"
                              [class.delivers]="c.delivers.length"
                              [class.cross-dp]="c.delivers_cross_dp"
                              [class.follow-target]="followTargets().has(c.course_id)"
                              [attr.data-course]="c.course_id"
                              [matTooltip]="courseTooltip(c)"
                              [routerLink]="['/learning/courses', c.course_id]"
                            >{{ c.course_code }}</a>
                          }
                        </span>
                      </li>
                    }
                  </ol>
                }
                </div>
              } @else if (cell.stage.planned && lane.track.kind === 'progression') {
                <div
                  class="node ghost"
                  aria-hidden="true"
                  [attr.data-node]="'planned:' + cell.stage.dp_order"
                >
                  <span class="node-eyebrow">DP {{ cell.stage.dp_order }}</span>
                  <span class="ghost-text">Not yet defined</span>
                </div>
              }
            </div>
          }
        }

        <!-- Transient course → follow-up arrows, drawn while a chip is hovered or
             focused. A second overlay, not part of .edges: qualification edges
             deliberately paint UNDER the cards, but these end at chips inside the
             cards, so they must paint over them. -->
        <svg
          class="course-edges"
          [attr.viewBox]="viewBox()"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <marker
              id="course-arrow"
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              markerUnits="userSpaceOnUse"
              orient="auto"
            >
              <path d="M 0 0 L 8 4 L 0 8 z" class="course-arrow-head" />
            </marker>
          </defs>
          @for (d of courseArrows(); track $index) {
            <path [attr.d]="d" class="course-edge" marker-end="url(#course-arrow)" />
          }
        </svg>
      </div>
    </div>

    <!-- Colour was carrying meaning with nothing to decode it. Three states, named. -->
    <p class="legend">
      <span class="legend-item">
        <span class="c-chip legend-chip">C101</span>
        teaches only
      </span>
      <span class="legend-item">
        <span class="c-chip legend-chip delivers">C204</span>
        satisfies an objective of this qualification
      </span>
      <span class="legend-item">
        <span class="c-chip legend-chip delivers cross-dp">C302</span>
        taught here, satisfies a later period's objective
      </span>
    </p>

    <p class="hint">
      <mat-icon>keyboard</mat-icon>
      Arrow keys move between qualifications, Enter opens one.
    </p>
  `,
  styles: [
    `
      :host { display: block; }

      .map-scroll { overflow-x: auto; padding-bottom: 6px; }

      /* The programme track under each node — the years/terms of the period. */
      .term-track {
        list-style: none;
        margin: 4px 0 0;
        padding: 8px 0 0 10px;
        border-left: 2px solid var(--border-light);
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .term-track .term { position: relative; }
      /* A node on the track, so a term reads as a stop along the path. */
      .term-track .term::before {
        content: '';
        position: absolute;
        left: -15px;
        top: 5px;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--border-strong, var(--text-muted));
      }
      .term-head {
        display: flex;
        align-items: baseline;
        gap: 6px;
        margin-bottom: 2px;
      }
      .term-name {
        font-size: 0.68rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
      }
      .term-count {
        font-size: 0.62rem;
        color: var(--text-muted);
        opacity: 0.8;
      }
      .term-courses { display: flex; flex-wrap: wrap; gap: 3px; }
      .c-chip {
        text-decoration: none;
        cursor: pointer;
        font-family: var(--font-mono, monospace);
        font-size: 0.62rem;
        line-height: 1.5;
        padding: 1px 5px;
        border-radius: 4px;
        border: 1px solid var(--border-light);
        color: var(--text-secondary, inherit);
        white-space: nowrap;
      }
      /* A course that carries a CFITES claim is the exception, so it is what stands out. */
      .c-chip.delivers {
        font-weight: 700;
        border-color: transparent;
        background: var(--accent-soft, rgba(0, 120, 90, 0.16));
        color: var(--text-primary, inherit);
      }
      .c-chip.cross-dp { background: var(--warn-soft, rgba(180, 110, 0, 0.18)); }
      .c-chip:hover { border-color: var(--accent); color: var(--accent); }
      .c-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 6px 18px;
        margin: 10px 0 0;
        font-size: 0.72rem;
        color: var(--text-muted);
      }
      .legend-item { display: inline-flex; align-items: center; gap: 6px; }
      .legend-chip { cursor: default; }

      .map {
        position: relative;
        display: grid;
        grid-template-columns: max-content repeat(var(--stage-count), minmax(210px, 1fr));
        /* The column gutter is where the connectors are drawn — too narrow and a
           branch curve has no room to bow out of the card it leaves. */
        column-gap: 34px;
        row-gap: 14px;
        align-items: stretch;
        min-width: min-content;
        padding: 4px;
      }

      /* ── Headers ───────────────────────────────────────────── */
      .corner { grid-column: 1; }
      .stage-head {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 6px 10px;
        border-bottom: 2px solid var(--border);
      }
      .stage-head.planned { border-bottom-style: dashed; opacity: 0.65; }
      .stage-head .dp {
        font-family: var(--font-display);
        font-weight: 700;
        letter-spacing: 0.04em;
        color: var(--accent);
      }
      .stage-head .rank { font-size: 0.78rem; color: var(--text-secondary); }
      .stage-head .stage-label { font-size: 0.72rem; color: var(--text-muted); }

      .lane-head {
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 2px;
        padding: 10px 14px 10px 0;
        max-width: 170px;
      }
      .lane-name {
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--text-secondary);
      }
      .lane-kind { font-size: 0.68rem; color: var(--text-muted); }
      .lane-head.specialty { border-left: 3px solid var(--warning); padding-left: 10px; }

      .cell { display: flex; min-height: 118px; }

      /* ── Edges ─────────────────────────────────────────────── */
      .edges {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        overflow: visible;
      }
      /* Motion on measured elements stays opacity-only: a transform animation (e.g.
         tnEnterStagger) during the first frame would corrupt the rects measure()
         anchors the edges to, and nothing re-measures once it settles. */
      .edge {
        fill: none;
        stroke: var(--border-light);
        stroke-width: 2;
        transition: opacity 0.18s ease;
        animation: edge-in 0.35s ease-out;
      }
      .edge[data-state='complete'] { stroke: var(--success); }
      .edge[data-state='in_progress'] { stroke: var(--accent); }
      .edge[data-state='locked'] { stroke: var(--border); }
      .edge.branch { stroke-dasharray: 1 0; opacity: 0.75; }
      .edge.planned { stroke-dasharray: 5 5; opacity: 0.45; }
      /* Only a from-frame: a to-frame with fill-mode would pin opacity at 1 and
         override the resting opacity of .branch and .planned above. */
      @keyframes edge-in { from { opacity: 0; } }

      .edge-arrow { fill: var(--border-light); }
      .edge-arrow[data-state='complete'] { fill: var(--success); }
      .edge-arrow[data-state='in_progress'] { fill: var(--accent); }
      .edge-arrow[data-state='locked'] { fill: var(--border); }
      .edge-arrow[data-state='planned'] { opacity: 0.45; }

      /* Tracing a bubble's prerequisite chain dims everything off it. Desktop only:
         below 900px the edges are hidden and touch has no hover to clear the dim. */
      @media (min-width: 901px) {
        .map.chain-active .node:not(.on-chain):not(.ghost),
        .map.chain-active .edge:not(.on-chain) { opacity: 0.3; }
        .map.chain-active .edge.on-chain { stroke-width: 2.5; }
      }

      /* Above the cards (themselves z-index 1): these arrows end at chips inside
         the cards, unlike the qualification edges painting underneath. */
      .course-edges {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        overflow: visible;
        z-index: 2;
      }
      .course-edge {
        fill: none;
        stroke: var(--accent);
        stroke-width: 1.5;
        opacity: 0.9;
        animation: edge-in 0.12s ease-out;
      }
      .course-arrow-head { fill: var(--accent); }
      .c-chip.follow-target { border-color: var(--accent); color: var(--accent); }

      /* ── Nodes ─────────────────────────────────────────────── */
      /* The bubble: the card face plus the programme taught in the period. */
      .node {
        position: relative;
        z-index: 1;
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 10px 12px;
        text-align: left;
        font: inherit;
        color: var(--text-primary);
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        box-shadow: var(--shadow-1);
        transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease,
          opacity 0.18s ease;
      }
      /* The card face is the control; the bubble around it is not. */
      .node-main {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 0;
        margin: 0;
        text-align: left;
        font: inherit;
        color: inherit;
        background: none;
        border: 0;
        cursor: pointer;
      }
      .node:has(.node-main:hover) { border-color: var(--accent); transform: translateY(-2px); }
      .node-main:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 3px;
        border-radius: var(--radius-sm, 4px);
      }
      .node.selected {
        border-color: var(--accent);
        box-shadow: var(--shadow-2), var(--glow-accent);
      }
      .node.state-locked { opacity: 0.58; }
      .node.state-complete { border-color: var(--success); }
      .node.ghost {
        align-items: center;
        justify-content: center;
        gap: 4px;
        background: transparent;
        border-style: dashed;
        box-shadow: none;
        cursor: default;
      }
      .ghost-text { font-size: 0.8rem; color: var(--text-muted); }

      .node-eyebrow {
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--text-muted);
      }
      .node-body { display: flex; align-items: center; gap: 10px; }
      .node-text { display: flex; flex-direction: column; min-width: 0; }
      .node-title {
        font-size: 0.82rem;
        font-weight: 600;
        color: var(--text-primary);
        overflow: hidden;
        text-overflow: ellipsis;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
      }

      .ring { width: 38px; height: 38px; flex: 0 0 38px; }
      .ring-track { fill: none; stroke: var(--border); stroke-width: 3; }
      .ring-fill {
        fill: none;
        stroke: var(--accent);
        stroke-width: 3;
        stroke-linecap: round;
        transform: rotate(-90deg);
        transform-origin: 50% 50%;
        transition: stroke-dashoffset 0.5s ease;
      }
      .state-complete .ring-fill { stroke: var(--success); }
      .ring-text {
        fill: var(--text-secondary);
        font-size: 11px;
        font-weight: 700;
        text-anchor: middle;
        dominant-baseline: central;
      }
      .ring-pct { font-size: 7px; opacity: 0.7; }

      .node-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
        font-size: 0.72rem;
        color: var(--text-muted);
      }
      .meta-item { display: inline-flex; align-items: center; gap: 3px; }
      .meta-item.gate { color: var(--warning); }
      /* An incomplete total should not read like a settled one. */
      .meta-item.partial {
        color: var(--warning);
        border-bottom: 1px dotted currentColor;
        cursor: help;
      }
      .node-meta mat-icon { font-size: 14px; width: 14px; height: 14px; }
      .state-icon { margin-left: auto; }
      .state-icon.done { color: var(--success); }

      .here-tag {
        position: absolute;
        top: -10px;
        right: 10px;
        /* Above the pulse halo below, which is an ::after and would otherwise
           paint its ring straight through this pill. */
        z-index: 2;
        padding: 2px 8px;
        font-size: 0.64rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--text-on-accent);
        background: var(--accent);
        border-radius: 999px;
        /* A ring in the card colour separates the pill from the border and the
           halo it sits across, so the two never appear to collide. */
        box-shadow: 0 0 0 3px var(--bg-card);
      }
      .node.here { border-color: var(--accent); }
      .node.here::after {
        content: '';
        position: absolute;
        inset: -4px;
        z-index: 0;
        border: 2px solid var(--accent);
        border-radius: calc(var(--radius-md) + 4px);
        opacity: 0.5;
        pointer-events: none;
        animation: here-pulse 2.4s ease-in-out infinite;
      }
      @keyframes here-pulse {
        0%, 100% { opacity: 0.5; transform: scale(1); }
        50% { opacity: 0.12; transform: scale(1.02); }
      }
      @media (prefers-reduced-motion: reduce) {
        .node.here::after { animation: none; }
        .node:has(.node-main:hover) { transform: none; }
        .ring-fill { transition: none; }
        .edge, .course-edge { animation: none; }
        .node, .edge { transition: none; }
      }

      .hint {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 8px 0 0;
        font-size: 0.74rem;
        color: var(--text-muted);
      }
      .hint mat-icon { font-size: 16px; width: 16px; height: 16px; }

      /* Narrow viewports: stack into a vertical rail rather than side-scrolling. */
      @media (max-width: 900px) {
        .map { grid-template-columns: 1fr; }
        .corner, .stage-head { display: none; }
        .lane-head {
          max-width: none;
          padding: 12px 0 4px;
          border-left: none;
          border-bottom: 1px solid var(--border);
        }
        .lane-head.specialty { padding-left: 0; border-left: none; }
        .edges { display: none; }
        .course-edges { display: none; }
        .cell { min-height: 0; }
        /* Stacked, an empty cell is just a gap — the grid no longer needs it to
           hold a column open. */
        .cell.is-empty { display: none; }
      }
    `,
  ],
})
export class CareerMapComponent implements AfterViewInit, OnDestroy {
  private readonly host = inject(ElementRef<HTMLElement>);
  private readonly zone = inject(NgZone);

  readonly ringR = RING_R;
  readonly ringCircumference = RING_CIRCUMFERENCE;

  /** Every state an edge can carry — one arrowhead marker is stamped per state. */
  protected readonly edgeStates: ReadonlyArray<NodeState | 'planned'> = [
    'locked', 'available', 'in_progress', 'complete', 'planned',
  ];

  @Input({ required: true }) set map(value: CurriculumMap | null) {
    this.data.set(value);
    this.focusIndex.set(0);
    // Cells move whenever the map changes, so re-measure — after the next frame,
    // once the new grid has actually been laid out.
    this.remeasureAfterLayout();
  }
  /** qsp_code of the open qualification. */
  @Input() selected: string | null = null;
  @Output() readonly selectedChange = new EventEmitter<string>();

  @ViewChild('grid') private gridRef?: ElementRef<HTMLElement>;
  @ViewChildren('cellButton') private buttons?: QueryList<ElementRef<HTMLButtonElement>>;

  protected readonly data = signal<CurriculumMap | null>(null);
  protected readonly focusIndex = signal(0);
  protected readonly drawnEdges = signal<DrawnEdge[]>([]);
  protected readonly viewBox = signal('0 0 0 0');

  protected readonly stages = computed(() => this.data()?.stages ?? []);
  protected readonly currentQsp = computed(() => this.data()?.learner?.current_qsp_code ?? null);

  /** The bubble whose incoming prerequisite chain is being traced, if any. */
  protected readonly chainSource = signal<string | null>(null);

  /** The traced chain: the source bubble plus every transitive prerequisite of it. */
  protected readonly chainNodes = computed<Set<string>>(() => {
    const src = this.chainSource();
    const map = this.data();
    if (!src || !map) return new Set<string>();
    const parents = new Map<string, string[]>();
    for (const e of map.edges) {
      const list = parents.get(e.to);
      if (list) list.push(e.from);
      else parents.set(e.to, [e.from]);
    }
    // BFS with a visited set, so a malformed cyclic edge list cannot hang the UI.
    const seen = new Set<string>([src]);
    const queue = [src];
    while (queue.length) {
      for (const p of parents.get(queue.pop()!) ?? []) {
        if (!seen.has(p)) {
          seen.add(p);
          queue.push(p);
        }
      }
    }
    return seen;
  });

  /** Course chip under the pointer (or keyboard focus), if any. */
  protected readonly hoveredCourse = signal<string | null>(null);
  /** Transient arrow paths from the hovered chip to its follow-up chips. */
  protected readonly courseArrows = signal<string[]>([]);
  /** The chips the hovered course leads into, for highlighting the target end. */
  protected readonly followTargets = computed<Set<string>>(() => {
    const src = this.hoveredCourse();
    if (!src) return new Set<string>();
    const edges = this.data()?.course_edges ?? [];
    return new Set(edges.filter(e => e.from === src).map(e => e.to));
  });

  /** Nodes bucketed into (lane, DP column) cells, with a flat index for keyboard nav. */
  protected readonly lanes = computed<Lane[]>(() => {
    const map = this.data();
    if (!map) return [];
    const stages = map.stages;
    let index = 0;
    return map.tracks.map(track => ({
      track,
      cells: stages.map(stage => {
        const node =
          map.nodes.find(n => n.track_key === track.key && n.dp_order === stage.dp_order) ?? null;
        // Only the rank ladder continues into the un-ingested periods; a specialty
        // stream has no claim on a DP nobody has written a QSP for.
        const ghost = !node && stage.planned && track.kind === 'progression';
        return {
          key: `${track.key}:${stage.dp_order}`,
          stage,
          trackKey: track.key,
          node,
          index: index++,
          filled: !!node || ghost,
        };
      }),
    }));
  });

  /** The flat, focusable order: left-to-right, lane by lane, skipping empty cells. */
  private readonly focusable = computed(() =>
    this.lanes().flatMap(lane => lane.cells.filter(c => c.node)),
  );

  private resizeObserver?: ResizeObserver;
  private pendingFrame = 0;

  ngAfterViewInit(): void {
    this.remeasureAfterLayout();
    // Measurement is pure DOM reading; keeping the observer out of the zone stops
    // every resize frame from kicking off a change-detection pass.
    this.zone.runOutsideAngular(() => {
      this.resizeObserver = new ResizeObserver(() => this.measure());
      if (this.gridRef) this.resizeObserver.observe(this.gridRef.nativeElement);
      // Chip hover is delegated to the grid rather than bound per chip: DP1 alone is
      // three years of courses, and per-chip bindings would run change detection on
      // every chip the pointer crosses. The zone is only entered when the hovered
      // course actually changes.
      const grid = this.gridRef?.nativeElement;
      if (grid) {
        grid.addEventListener('mouseover', this.onChipEnter);
        grid.addEventListener('focusin', this.onChipEnter);
        grid.addEventListener('mouseout', this.onChipLeave);
        grid.addEventListener('focusout', this.onChipLeave);
      }
    });
    this.host.nativeElement.addEventListener('keydown', this.onKeydown);
  }

  ngOnDestroy(): void {
    this.resizeObserver?.disconnect();
    cancelAnimationFrame(this.pendingFrame);
    this.host.nativeElement.removeEventListener('keydown', this.onKeydown);
    const grid = this.gridRef?.nativeElement;
    if (grid) {
      grid.removeEventListener('mouseover', this.onChipEnter);
      grid.removeEventListener('focusin', this.onChipEnter);
      grid.removeEventListener('mouseout', this.onChipLeave);
      grid.removeEventListener('focusout', this.onChipLeave);
    }
  }

  /** Measure on the next frame, once the browser has laid the new grid out. */
  private remeasureAfterLayout(): void {
    if (typeof requestAnimationFrame !== 'function') return;
    cancelAnimationFrame(this.pendingFrame);
    this.zone.runOutsideAngular(() => {
      this.pendingFrame = requestAnimationFrame(() => this.measure());
    });
  }

  // ── Edge geometry ───────────────────────────────────────────

  /**
   * Resolve each logical edge to a path between the two laid-out cards.
   *
   * Progression edges leave the right edge of the source and enter the left edge
   * of the target; branch edges leave lower down so a fork is visually distinct
   * from a straight continuation.
   */
  private measure(): void {
    const grid = this.gridRef?.nativeElement;
    const map = this.data();
    if (!grid || !map) return;

    const origin = grid.getBoundingClientRect();
    this.viewBox.set(`0 0 ${origin.width} ${origin.height}`);

    const boxes = new Map<string, DOMRect>();
    grid.querySelectorAll<HTMLElement>('[data-node]').forEach(el => {
      const code = el.dataset['node'];
      if (code) boxes.set(code, el.getBoundingClientRect());
    });

    const stateOf = (code: string): NodeState | 'planned' =>
      code.startsWith('planned:')
        ? 'planned'
        : map.nodes.find(n => n.qsp_code === code)?.state ?? 'locked';

    const drawn: DrawnEdge[] = [];
    for (const edge of map.edges) {
      const from = boxes.get(edge.from);
      const to = boxes.get(edge.to);
      if (!from || !to) continue;

      const x2 = to.left - origin.left;
      const y2 = to.top + to.height / 2 - origin.top;

      if (edge.kind === 'branch') {
        // A fork leaves the bottom of the card and drops into the lane below, so
        // it reads as a departure from the ladder rather than a continuation of it.
        const x1 = from.left + from.width / 2 - origin.left;
        const y1 = from.bottom - origin.top;
        const drop = Math.max(20, (y2 - y1) * 0.55);
        drawn.push({
          kind: edge.kind,
          state: stateOf(edge.to),
          from: edge.from,
          to: edge.to,
          path: `M ${x1} ${y1} C ${x1} ${y1 + drop}, ${x2 - 40} ${y2}, ${x2} ${y2}`,
        });
        continue;
      }

      // Straight continuations run card-edge to card-edge across the gutter.
      const x1 = from.right - origin.left;
      const y1 = from.top + from.height / 2 - origin.top;
      const bend = Math.max(18, (x2 - x1) / 2);
      drawn.push({
        kind: edge.kind,
        state: stateOf(edge.to),
        from: edge.from,
        to: edge.to,
        path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
      });
    }
    this.zone.run(() => {
      this.drawnEdges.set(drawn);
      // Chip rects move with the layout, so any transient arrow is now stale.
      if (this.courseArrows().length) {
        this.courseArrows.set([]);
        this.hoveredCourse.set(null);
      }
    });
  }

  // ── Course follow-up arrows ─────────────────────────────────

  /** Delegated hover/focus onto a course chip: draw its follow-up arrows. */
  private readonly onChipEnter = (event: Event): void => {
    const chip = (event.target as HTMLElement | null)?.closest?.(
      '.c-chip[data-course]',
    ) as HTMLElement | null;
    const id = chip?.dataset['course'] ?? null;
    if (!id || id === this.hoveredCourse()) return;
    const arrows = this.measureCourseArrows(id);
    this.zone.run(() => {
      this.hoveredCourse.set(id);
      this.courseArrows.set(arrows);
    });
  };

  /** Clear the arrows, unless the pointer/focus only moved within the same chip. */
  private readonly onChipLeave = (event: Event): void => {
    const current = this.hoveredCourse();
    if (!current) return;
    const rel = (event as MouseEvent | FocusEvent).relatedTarget as HTMLElement | null;
    if (rel?.closest?.(`.c-chip[data-course="${current}"]`)) return;
    this.zone.run(() => {
      this.hoveredCourse.set(null);
      this.courseArrows.set([]);
    });
  };

  /**
   * Arrows from a chip to each of its follow-up chips, measured at hover time.
   * Chips flex-wrap, so their rects are only valid for the current layout — nothing
   * is cached, and `measure()` clears the arrows whenever the grid moves.
   */
  private measureCourseArrows(courseId: string): string[] {
    const grid = this.gridRef?.nativeElement;
    const map = this.data();
    if (!grid || !map) return [];
    const source = grid.querySelector<HTMLElement>(`.c-chip[data-course="${courseId}"]`);
    if (!source) return [];

    const origin = grid.getBoundingClientRect();
    const s = source.getBoundingClientRect();
    const arrows: string[] = [];
    for (const edge of map.course_edges ?? []) {
      if (edge.from !== courseId) continue;
      const target = grid.querySelector<HTMLElement>(`.c-chip[data-course="${edge.to}"]`);
      if (!target) continue; // the backend only ships on-map ids, but stay defensive
      arrows.push(this.chipPath(s, target.getBoundingClientRect(), origin));
    }
    return arrows;
  }

  /** The same curve families as the qualification edges, chip-sized. */
  private chipPath(s: DOMRect, t: DOMRect, origin: DOMRect): string {
    const dx = t.left + t.width / 2 - (s.left + s.width / 2);
    const dy = t.top + t.height / 2 - (s.top + s.height / 2);
    if (Math.abs(dx) >= Math.abs(dy)) {
      // Horizontal-dominant: leave one side, enter the facing side of the target.
      const x1 = (dx >= 0 ? s.right : s.left) - origin.left;
      const y1 = s.top + s.height / 2 - origin.top;
      const x2 = (dx >= 0 ? t.left : t.right) - origin.left;
      const y2 = t.top + t.height / 2 - origin.top;
      const bend = Math.max(20, Math.abs(x2 - x1) / 2) * (dx >= 0 ? 1 : -1);
      return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
    }
    // Vertical-dominant: bottom-centre to top-centre (mirrored going up), so the
    // terminal tangent stays vertical and the arrowhead points into the chip.
    const x1 = s.left + s.width / 2 - origin.left;
    const y1 = (dy >= 0 ? s.bottom : s.top) - origin.top;
    const x2 = t.left + t.width / 2 - origin.left;
    const y2 = (dy >= 0 ? t.top : t.bottom) - origin.top;
    const drop = Math.max(16, Math.abs(y2 - y1) * 0.45) * (dy >= 0 ? 1 : -1);
    return `M ${x1} ${y1} C ${x1} ${y1 + drop}, ${x2} ${y2 - drop}, ${x2} ${y2}`;
  }

  // ── Interaction ─────────────────────────────────────────────

  protected pick(node: QualNode, index: number): void {
    this.focusIndex.set(index);
    this.selectedChange.emit(node.qsp_code);
  }

  /**
   * Roving-tabindex navigation. Left/Right walk the DP columns, Up/Down cross
   * lanes, Home/End jump to the ends — all over the flat list of real nodes, so
   * empty cells never trap focus.
   */
  private readonly onKeydown = (event: KeyboardEvent): void => {
    const cells = this.focusable();
    if (!cells.length) return;

    const laneCount = this.lanes().length;
    const stageCount = this.stages().length;
    const at = cells.findIndex(c => c.index === this.focusIndex());
    if (at < 0) return;

    let next = at;
    switch (event.key) {
      case 'ArrowRight': next = at + 1; break;
      case 'ArrowLeft': next = at - 1; break;
      case 'ArrowDown':
      case 'ArrowUp': {
        // Move to the nearest node in an adjacent lane at or after this column.
        const current = cells[at];
        const step = event.key === 'ArrowDown' ? 1 : -1;
        const lane = Math.floor(current.index / stageCount);
        for (let l = lane + step; l >= 0 && l < laneCount; l += step) {
          const found = cells.find(c => Math.floor(c.index / stageCount) === l);
          if (found) { next = cells.indexOf(found); break; }
        }
        break;
      }
      case 'Home': next = 0; break;
      case 'End': next = cells.length - 1; break;
      default: return;
    }

    event.preventDefault();
    if (next < 0 || next >= cells.length) return;

    this.focusIndex.set(cells[next].index);
    // `buttons` is in DOM order, which is the same order as `focusable()`, so the
    // nth focusable cell is the nth button.
    this.buttons?.get(next)?.nativeElement.focus();
  };

  // ── Presentation helpers ────────────────────────────────────

  protected ringOffset(node: QualNode): number {
    return RING_CIRCUMFERENCE * (1 - (node.progress.pct || 0) / 100);
  }

  protected fmtHours(minutes: number): string {
    const h = Math.round((minutes / 60) * 10) / 10;
    return h >= 1 ? `${h} h` : `${minutes} min`;
  }

  /** The programme for a node, grouped into the terms that teach it. */
  termsOf(node: QualNode): TermGroup[] {
    return groupByTerm(node.courses);
  }

  /** What a course chip says on hover: its name, then any objective it delivers. */
  courseTooltip(c: NodeCourse): string {
    const lines = [c.name];
    for (const d of c.delivers) {
      lines.push(`delivers ${d.qsp_code} ${d.po_code} — ${d.po_title}`);
    }
    if (!c.delivers.length) lines.push('delivers no performance objective');
    return lines.join('\n');
  }

  /** Taught hours for the period, thousands-separated. */
  protected fmtCourseHours(node: QualNode): string {
    return `${node.course_hours.toLocaleString()} h`;
  }

  protected programmeTooltip(node: QualNode): string {
    const inst = node.courses[0]?.institution;
    const where = inst ? `${inst}: ` : '';
    return (
      `${where}${node.course_count} courses totalling ${node.course_hours.toLocaleString()} ` +
      'taught hours in this period. Separate from assessment time, which is how long ' +
      'being tested against the objectives takes.'
    );
  }

  /** True when some objectives carry no duration, so the total is only a floor. */
  protected isPartiallyTimed(node: QualNode): boolean {
    return node.timed_po_count < node.po_count;
  }

  protected durationTooltip(node: QualNode): string {
    return this.isPartiallyTimed(node)
      ? `${node.timed_po_count} of ${node.po_count} objectives have a duration — ` +
        'the total is a floor, not the full assessment load'
      : `Total assessment time across ${node.po_count} objectives`;
  }

  /** The whole node state spelled out, so the map is usable without seeing it. */
  protected ariaLabel(node: QualNode): string {
    const parts = [
      `DP ${node.dp_order}${node.rank_level ? `, ${node.rank_level}` : ''}.`,
      `${node.qsp_code} — ${node.title}.`,
      `${STATE_LABEL[node.state]}, ${node.progress.completed} of ${node.po_count} objectives complete.`,
    ];
    if (node.gate_count) parts.push(`${node.gate_count} gating assessment${node.gate_count > 1 ? 's' : ''}.`);
    if (node.qsp_code === this.currentQsp()) parts.push('Current position.');
    return parts.join(' ');
  }
}
