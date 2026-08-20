import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import {
  CompTag,
  EO,
  NodeCourse,
  PO,
  POProgressState,
  QualNode,
  TermGroup,
  groupByTerm,
} from '@core/services/curriculum-map.service';


const PROGRESS_LABEL: Record<POProgressState, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  completed: 'Complete',
  failed: 'Not met',
};

/**
 * The selected qualification, broken down into performance objectives, their
 * enabling objectives, and the lessons behind them.
 *
 * Only the open qualification renders — the career map is the index, so there is
 * no reason to hold every objective on the page at once.
 */
@Component({
  selector: 'tn-qualification-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, MatCardModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (qualification; as q) {
      <section class="detail">
        <header class="detail-head">
          <div>
            <!-- The qualification and the programme that delivers it are the same
                 thing: ALJQ *is* the Algonquin curriculum, ACPZ *is* the RMC year.
                 Naming both read as two separate entities. The identifiers stay on the
                 title as a tooltip so nothing is lost. -->
            <h3 [matTooltip]="identifiers(q)">{{ q.title }}</h3>
            <p class="muted">
              @if (institution(q)) { {{ institution(q) }} · }
              {{ q.po_count }} performance objectives
              @if (q.course_count) {
                · {{ q.course_count }} courses
                @if (q.course_hours) { · {{ q.course_hours.toLocaleString() }} taught hours }
              }
              <!-- Assessment time is not the length of the programme. Labelled, or 36 h
                   reads as the duration of three years of study. -->
              @if (q.total_minutes) {
                · {{ fmtDuration(q.total_minutes) }} assessed
              }
            </p>
          </div>
          <span class="node-state" [class]="'state-' + q.state">{{ stateLabel(q) }}</span>
        </header>

        @if (q.courses.length) {
          <section class="programme">
            <p class="muted programme-note">
              Every course taught in this period, by term. Most teach without satisfying
              a performance objective; the ones that do name it.
            </p>

            @for (term of termsOf(q); track term.code) {
              <div class="term">
                <div class="term-head">{{ term.label || term.code }}</div>
                <ul class="course-list">
                  @for (c of term.courses; track c.course_id) {
                    <li class="course">
                      <a
                        class="c-code"
                        [routerLink]="['/learning/courses', c.course_id]"
                        [matTooltip]="'Open ' + c.course_code + ' in the course catalogue'"
                      >{{ c.course_code }}</a>
                      <span class="c-name">{{ courseTitle(c) }}</span>
                      <span class="badge diff" [class]="'d-' + c.difficulty">{{ c.difficulty }}</span>
                      @if (c.duration_hours) {
                        <span class="badge dur">{{ c.duration_hours }}h</span>
                      }
                      @if (c.delivers.length) {
                        <span
                          class="badge delivers"
                          [class.cross-dp]="c.delivers_cross_dp"
                        >{{ deliversLabel(c) }}</span>
                      }
                    </li>

                    <!-- The objective lives with the module that satisfies it, rather
                         than in a parallel list repeating the same 15 relationships. -->
                    @for (po of objectivesFor(q, c); track po.po_code) {
                      <li class="course-objective">
                        <ng-container
                          [ngTemplateOutlet]="objectiveCard"
                          [ngTemplateOutletContext]="{ $implicit: po }"
                        />
                      </li>
                    }
                  }
                </ul>
              </div>
            }
          </section>
        }

        <!-- Objectives this qualification owns but another period teaches. Without
             this they would vanish: the specialty streams are entirely delivered by
             Algonquin Year-3 courses, which are placed on DP1. -->
        @if (deliveredElsewhere(q).length) {
          <section class="elsewhere">
            <h4 class="section-heading">Taught in another period</h4>
            <p class="muted programme-note">
              These objectives belong to this qualification but are delivered by courses
              taught earlier in the path.
            </p>
            @for (po of deliveredElsewhere(q); track po.po_code) {
              <div class="from-where">
                <span class="muted">via</span>
                <a
                  class="c-code"
                  [routerLink]="['/learning/courses', po.delivered_by!.course_id]"
                >{{ po.delivered_by!.course_code }}</a>
                <span class="muted">
                  {{ po.delivered_by!.module_title }} · DP{{ po.delivered_by!.dp_order }}
                </span>
              </div>
              <ng-container
                [ngTemplateOutlet]="objectiveCard"
                [ngTemplateOutletContext]="{ $implicit: po }"
              />
            }
          </section>
        }

        <!-- A gap has to be visible as a gap. An objective with nothing real behind it
             can never show progress, however much courseware exists elsewhere. -->
        @if (undelivered(q).length) {
          <section class="gap">
            <h4 class="section-heading warn">
              Not yet delivered ({{ undelivered(q).length }})
            </h4>
            <p class="muted programme-note">
              No course module satisfies these yet, so no learner can make progress
              against them.
            </p>
            @for (po of undelivered(q); track po.po_code) {
              <ng-container
                [ngTemplateOutlet]="objectiveCard"
                [ngTemplateOutletContext]="{ $implicit: po }"
              />
            }
          </section>
        }
      </section>
    }

    <!-- One objective card, rendered wherever the objective belongs: under the course
         that delivers it, under a pointer to another period, or in the gap list. -->
    <ng-template #objectiveCard let-po>
          <mat-card class="po-card" [class.is-current]="po.po_code === currentPoCode">
            <div class="po-top">
              <span class="po-code" [title]="po.po_code">{{ po.course_code || po.po_code }}</span>
              <span class="po-title">{{ po.title }}</span>

              <span class="badge progress" [class]="'p-' + po.progress_state">
                {{ progressLabel(po) }}
              </span>
              <span class="badge" [class.gate]="po.tier === 'gate'">{{ po.tier }}</span>
              @if (po.duration_min) {
                <span class="badge dur">{{ fmtDuration(po.duration_min) }}</span>
              } @else {
                <span
                  class="badge unscoped"
                  matTooltip="No duration in the QSP crosswalk — this objective is not scoped yet"
                >no duration</span>
              }
              @if (po.duration_long) {
                <span
                  class="badge warn"
                  matTooltip="Unusually long assessment (8h+) — verify the duration is intentional"
                >⚠ long</span>
              }
              @if (po.exercise_id) {
                <a
                  class="ex-link"
                  [routerLink]="['/exercises', po.exercise_id]"
                  title="Individual practical assessment"
                >Open assessment <mat-icon>chevron_right</mat-icon></a>
              }
            </div>

            <div class="tags">
              @for (c of nice(po); track c.code) {
                <span
                  class="tag nice"
                  [matTooltip]="'NICE/DCWF work role ' + c.code + ' — the role this objective builds toward'"
                >{{ c.name }} ({{ c.code }})</span>
              }
              <!-- NIST CSF functions render as bare two-letter codes, which mean
                   nothing without the name; the supporting categories underneath
                   them are what actually say which activities are covered. -->
              @for (c of nist(po); track c.code) {
                <span class="tag nist" [matTooltip]="nistTooltip(po, c)">
                  {{ c.code }} · {{ c.name }}
                </span>
              }
              @for (c of nistCategories(po); track c.code) {
                <span
                  class="tag nist-cat"
                  [matTooltip]="'NIST CSF 2.0 category ' + c.code + ' under ' + functionName(po, c)"
                >{{ c.name }}</span>
              }
            </div>

            <!-- The assessment brief: what they get, how they're tested, what
                 they must find, what they hand in, and what passes. -->
            <dl class="brief">
              @if (po.conditions) {
                <div><dt>Given</dt><dd>{{ po.conditions }}</dd></div>
              }
              @if (po.assessment_type) {
                <div><dt>Assessed by</dt><dd>{{ po.assessment_type }}</dd></div>
              }
              @if (po.critical_events.length) {
                <div>
                  <dt>Must find</dt>
                  <dd>{{ po.critical_events.join(', ') }}</dd>
                </div>
              }
              @if (po.deliverable) {
                <div><dt>Hands in</dt><dd>{{ po.deliverable }}</dd></div>
              }
              @if (po.pass_standard) {
                <div><dt>Passes at</dt><dd>{{ po.pass_standard }}</dd></div>
              }
              @if (po.scenario_count) {
                <div>
                  <dt>Scenarios</dt>
                  <dd>{{ po.scenario_count }}</dd>
                </div>
              }
            </dl>

            @if (needsScoping(po)) {
              <p class="scoping-gap">
                <mat-icon>report_problem</mat-icon>
                <span>
                  {{ fmtDuration(po.duration_min) }} of assessment with no enabling objectives —
                  the QSP crosswalk gives no breakdown of what this covers. The detail lives in the
                  QSP chapter, which is read on-box and never ingested here.
                </span>
              </p>
            }

            @if (po.enabling_objectives.length) {
              <ul class="eos">
                @for (eo of po.enabling_objectives; track eo.eo_code) {
                  <li>
                    <span class="eo-code">EO {{ eo.eo_code }}</span> {{ eo.title }}
                    @if (eo.lessons.length) {
                      <span class="lesson-pill">
                        <mat-icon>menu_book</mat-icon>{{ eo.lessons.length }} lesson(s)
                        @if (!anyPublished(eo)) { <em>· draft</em> }
                      </span>
                    }
                  </li>
                }
              </ul>
            }
          </mat-card>
    </ng-template>
  `,
  styles: [
    `
      :host { display: block; }
      .muted { color: var(--text-muted); }

      .delivered-by {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 8px;
        margin: 8px 0 4px;
        font-size: 0.82rem;
      }
      .delivered-by .db-label {
        color: var(--text-muted);
        text-transform: uppercase;
        font-size: 0.68rem;
        letter-spacing: 0.05em;
        font-weight: 700;
      }
      .delivered-by .db-course { font-weight: 700; font-family: var(--font-mono, monospace); }
      .delivered-by .db-module { color: var(--text-secondary, inherit); }
      .delivered-by.none .db-label { color: var(--warn, #b26a00); }

      .section-heading {
        margin: 22px 0 8px;
        font-size: 0.92rem;
        border-top: 1px solid var(--border-light);
        padding-top: 14px;
      }
      .programme { margin-top: 4px; }
      .course-objective { list-style: none; margin: 4px 0 10px 62px; }
      .course-objective .po-card { margin: 0; }
      .elsewhere, .gap { margin-top: 18px; }
      .section-heading.warn { color: var(--warn, #b26a00); }
      .from-where {
        display: flex;
        align-items: baseline;
        gap: 6px;
        margin: 10px 0 2px;
        font-size: 0.78rem;
      }
      .from-where .c-code {
        font-family: var(--font-mono, monospace);
        font-weight: 700;
        color: inherit;
        text-decoration: none;
      }
      .from-where .c-code:hover { color: var(--accent); text-decoration: underline; }
      .programme h4 { margin: 0 0 2px; font-size: 0.92rem; }
      .programme-note { margin: 0 0 12px; font-size: 0.78rem; }

      .term { margin-bottom: 12px; }
      .term-head {
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
        margin-bottom: 4px;
      }
      .course-list { list-style: none; margin: 0; padding: 0; }
      .course {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 8px;
        padding: 4px 0;
        border-bottom: 1px solid var(--border-subtle, transparent);
        font-size: 0.82rem;
      }
      .course .c-code {
        flex: 0 0 auto;
        min-width: 62px;
        font-family: var(--font-mono, monospace);
        font-weight: 700;
        color: inherit;
        text-decoration: none;
      }
      .course .c-code:hover { color: var(--accent); text-decoration: underline; }
      .course .c-name { flex: 1 1 auto; }
      .badge.delivers { background: var(--accent-soft, rgba(0, 120, 90, 0.12)); font-weight: 700; }
      .badge.delivers.cross-dp,
      .badge.cross-dp { background: var(--warn-soft, rgba(180, 110, 0, 0.15)); }

      .detail-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
      }
      .detail-head h3 { margin: 0 0 2px; font-size: 1.05rem; }
      .detail-head p { margin: 0; font-size: 0.82rem; }

      .node-state {
        flex: 0 0 auto;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        border: 1px solid var(--border-light);
        color: var(--text-secondary);
      }
      .node-state.state-complete { color: var(--success); border-color: var(--success); }
      .node-state.state-in_progress { color: var(--accent); border-color: var(--accent); }
      .node-state.state-locked { color: var(--text-muted); }

      .po-card {
        padding: 12px 14px;
        margin: 10px 0;
        background: var(--bg-card);
        border: 1px solid var(--border);
      }
      .po-card.is-current { border-color: var(--accent); box-shadow: var(--glow-accent); }

      .po-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .po-code { font-family: var(--font-display); font-weight: 700; }
      .po-title { flex: 1 1 auto; }

      .badge {
        font-size: 0.72rem;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--bg-surface);
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.03em;
      }
      .badge.gate { background: var(--accent-muted); color: var(--warning); }
      .badge.env { background: var(--accent-muted); color: var(--accent); }
      .badge.dur { background: transparent; color: var(--text-muted); }
      .badge.unscoped {
        background: transparent;
        color: var(--warning);
        border: 1px dashed currentColor;
        cursor: help;
      }
      .badge.warn { background: var(--accent-muted); color: var(--warning); font-weight: 700; }
      .badge.progress { border: 1px solid var(--border-light); background: transparent; }
      .badge.progress.p-completed { color: var(--success); border-color: var(--success); }
      .badge.progress.p-in_progress { color: var(--accent); border-color: var(--accent); }
      .badge.progress.p-failed { color: var(--alert); border-color: var(--alert); }
      .badge.progress.p-not_started { color: var(--text-muted); }

      .ex-link {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        margin-left: auto;
        font-size: 0.78rem;
        color: var(--accent);
        text-decoration: none;
        font-weight: 600;
      }
      .ex-link:hover { text-decoration: underline; }
      .ex-link mat-icon { font-size: 16px; width: 16px; height: 16px; }

      .tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
      .tag {
        font-size: 0.74rem;
        padding: 2px 9px;
        border-radius: 12px;
        border: 1px solid transparent;
      }
      .tag.nice { background: var(--accent-muted); border-color: var(--accent); }
      .tag.nist {
        background: var(--bg-surface);
        border-color: var(--border-light);
        font-weight: 600;
        cursor: help;
      }
      .tag.nist-cat {
        background: transparent;
        border-color: var(--border);
        color: var(--text-muted);
        cursor: help;
      }
      .tag.nice { cursor: help; }

      .brief {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
        gap: 4px 18px;
        margin: 10px 0 0;
        padding: 10px 12px;
        background: var(--bg-surface);
        border-radius: var(--radius-sm);
        font-size: 0.82rem;
      }
      .brief:empty { display: none; }
      .brief > div { display: flex; gap: 8px; align-items: baseline; }
      .brief dt {
        flex: 0 0 auto;
        min-width: 76px;
        color: var(--text-muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .brief dd { margin: 0; color: var(--text-primary); }

      .scoping-gap {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        margin: 10px 0 0;
        padding: 8px 12px;
        font-size: 0.8rem;
        color: var(--text-secondary);
        background: var(--accent-muted);
        border-left: 3px solid var(--warning);
        border-radius: var(--radius-sm);
      }
      .scoping-gap mat-icon {
        flex: 0 0 auto;
        color: var(--warning);
        font-size: 18px;
        width: 18px;
        height: 18px;
      }
      .eos { margin: 8px 0 0; padding-left: 18px; color: var(--text-secondary); }
      .eos li { margin: 3px 0; }
      .eo-code { font-weight: 600; color: var(--text-primary); }
      .lesson-pill {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        margin-left: 8px;
        font-size: 0.75rem;
        color: var(--text-muted);
      }
      .lesson-pill mat-icon { font-size: 15px; width: 15px; height: 15px; }
    `,
  ],
})
export class QualificationDetailComponent {
  @Input() qualification: QualNode | null = null;
  /** Highlights the objective the learner is currently on. */
  @Input() currentPoCode: string | null = null;

  /** Minutes -> human-readable ("8 h", "13 h", "1 day (24 h)"). */
  /** Objectives this course delivers, as full objective records.
   *
   * `NodeCourse.delivers` carries only codes; the assessment brief lives on the node's
   * objectives. Joining them here is what lets the objective render under the module
   * that satisfies it rather than in a parallel list.
   */
  objectivesFor(q: QualNode, c: NodeCourse): PO[] {
    const codes = new Set(c.delivers.map((d) => d.po_code));
    return q.objectives.filter(
      (po) => codes.has(po.po_code) && po.delivered_by?.course_id === c.course_id,
    );
  }

  /**
   * Objectives delivered by a course taught in a different period.
   *
   * The specialty streams are entirely delivered by Algonquin Year-3 courses, which are
   * placed on DP1 where they are taught — so without this every ALRA and Red Analyst
   * objective would disappear from its own qualification.
   */
  deliveredElsewhere(q: QualNode): PO[] {
    const own = new Set(q.courses.map((c) => c.course_id));
    return q.objectives.filter(
      (po) => po.delivered_by && !po.delivered_by.is_placeholder && !own.has(po.delivered_by.course_id),
    );
  }

  /** Objectives nothing real satisfies. A stub deliverer counts as nothing. */
  undelivered(q: QualNode): PO[] {
    return q.objectives.filter((po) => !po.delivered_by || po.delivered_by.is_placeholder);
  }

  /** The programme for this qualification, grouped into terms. Shared with the
   * career map so the two views divide a period identically. */
  termsOf(q: QualNode): TermGroup[] {
    return groupByTerm(q.courses);
  }

  /** The course name without its leading code, which is shown separately. */
  courseTitle(c: NodeCourse): string {
    const sep = c.name.indexOf(' — ');
    return sep === -1 ? c.name : c.name.slice(sep + 3);
  }

  deliversTooltip(c: NodeCourse): string {
    return c.delivers
      .map((d) => `${d.qsp_code} ${d.po_code} — ${d.po_title}`)
      .join('\n');
  }

  /** The institution whose programme delivers this qualification. */
  institution(q: QualNode): string {
    return q.courses[0]?.institution ?? '';
  }

  /** The codes behind the title, kept off the face but not thrown away. */
  identifiers(q: QualNode): string {
    const parts = [q.qsp_code];
    if (q.nqual && q.nqual !== q.qsp_code) parts.push(`NQual ${q.nqual}`);
    return parts.join(' · ');
  }

  /**
   * Which objectives a course satisfies — the part that carries a qualification claim.
   *
   * A bare count said "how many" and never "which", which is the only thing a reader
   * can act on. Objectives from one qualification are listed by code; the qualification
   * is named only when it is not the one being viewed, which is what makes a course
   * taught here but counting toward another period legible.
   */
  deliversLabel(c: NodeCourse): string {
    const quals = new Set(c.delivers.map((d) => d.qsp_code));
    const codes = c.delivers.map((d) => d.po_code.replace(/^PO_/, 'PO ')).join(', ');
    if (quals.size === 1 && c.delivers_cross_dp) {
      return `${[...quals][0]} · ${codes}`;
    }
    if (quals.size > 1) return `${c.delivers.length} objectives`;
    return codes;
  }

  fmtDuration(min: number): string {
    if (!min) return '';
    if (min < 60) return `${min} min`;
    const h = min / 60;
    if (min % 60 === 0) {
      if (min >= 1440 && min % 1440 === 0) {
        const d = min / 1440;
        return `${d} day${d > 1 ? 's' : ''} (${h} h)`;
      }
      return `${h} h`;
    }
    return `${Math.floor(min / 60)}h ${min % 60}m`;
  }

  nice(po: PO): CompTag[] {
    return (po.competencies ?? []).filter(c => c.framework === 'nice' && c.relation === 'primary');
  }

  /** CSF functions — the six top-level codes (GV/ID/PR/DE/RS/RC). */
  nist(po: PO): CompTag[] {
    return (po.competencies ?? []).filter(
      c => c.framework === 'nist_csf' && c.relation === 'primary',
    );
  }

  /** CSF categories (e.g. DE.AE) — the activity-level detail under each function. */
  nistCategories(po: PO): CompTag[] {
    return (po.competencies ?? []).filter(
      c => c.framework === 'nist_csf' && c.relation === 'supporting',
    );
  }

  /** Name of the function a category rolls up to, from its `XX.YY` code prefix. */
  functionName(po: PO, category: CompTag): string {
    const prefix = category.code.split('.')[0];
    return this.nist(po).find(f => f.code === prefix)?.name ?? prefix;
  }

  nistTooltip(po: PO, fn: CompTag): string {
    const under = this.nistCategories(po)
      .filter(c => c.code.startsWith(`${fn.code}.`))
      .map(c => c.name);
    const head = `NIST CSF 2.0 function ${fn.code} — ${fn.name}`;
    return under.length ? `${head}. Covers: ${under.join(', ')}` : head;
  }

  anyPublished(eo: EO): boolean {
    return (eo.lessons ?? []).some(l => l.is_published);
  }

  /** A long assessment with no enabling objectives has nothing explaining what it covers. */
  needsScoping(po: PO): boolean {
    return po.duration_min > 0 && !po.enabling_objectives.length;
  }

  progressLabel(po: PO): string {
    return PROGRESS_LABEL[po.progress_state] ?? PROGRESS_LABEL.not_started;
  }

  stateLabel(q: QualNode): string {
    return q.state === 'in_progress' ? 'In progress' : q.state;
  }
}
