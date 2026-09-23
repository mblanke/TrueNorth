import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatExpansionModule } from '@angular/material/expansion';
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

/** The shared status-chip look for each objective state. */
const PROGRESS_CHIP: Record<POProgressState, string> = {
  not_started: 'pending',
  in_progress: 'active',
  completed: 'completed',
  failed: 'failed',
};

/**
 * Objectives nothing real satisfies. A stub deliverer counts as nothing.
 *
 * Exported because the career path shows this count on a stage's collapsed row: a gap
 * must stay visible without having to open anything.
 */
export function undeliveredObjectives(q: QualNode): PO[] {
  return q.objectives.filter((po) => !po.delivered_by || po.delivered_by.is_placeholder);
}

/**
 * A qualification's programme and objectives, shown inside its (expanded) row on the
 * career path. Everything folds: terms, objectives, and enabling objectives, so the
 * open stage reads as a short list rather than a wall of cards.
 *
 * Only an open stage renders this, so there is no reason to hold every objective on
 * the page at once.
 */
@Component({
  selector: 'tn-qualification-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, MatExpansionModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (qualification; as q) {
      <section class="detail">
        <!-- The title and state are on the row this sits inside, so only the facts
             that don't fit there. The qualification and the programme that delivers it
             are the same thing (ALJQ *is* the Algonquin curriculum), so they are not
             named twice; the identifiers stay available as a tooltip. -->
        <p class="tn-muted tn-small qd-meta" [matTooltip]="identifiers(q)">
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

        @if (q.courses.length) {
          <h4 class="tn-section-label">Programme</h4>
          <mat-accordion multi displayMode="flat" class="terms">
            @for (term of termsOf(q); track term.code) {
              <mat-expansion-panel class="term">
                <mat-expansion-panel-header>
                  <mat-panel-title>{{ term.label || term.code }}</mat-panel-title>
                  <mat-panel-description class="tn-small">
                    {{ term.courses.length }} {{ term.courses.length === 1 ? 'course' : 'courses' }}
                    @if (termHours(term)) { · {{ termHours(term).toLocaleString() }} h }
                  </mat-panel-description>
                </mat-expansion-panel-header>
                <ng-template matExpansionPanelContent>
                  <ul class="course-list">
                    @for (c of term.courses; track c.course_id) {
                      <li class="course">
                        <a
                          class="tn-code c-code"
                          [routerLink]="['/learning/courses', c.course_id]"
                          [matTooltip]="'Open ' + c.course_code"
                        >{{ c.course_code }}</a>
                        <span class="c-name">{{ courseTitle(c) }}</span>
                        <span class="c-meta tn-muted tn-small">
                          <span class="cap">{{ c.difficulty }}</span>@if (c.duration_hours) { · {{ c.duration_hours }} h }
                        </span>
                        @if (!c.is_published) {
                          <span class="draft-pill" matTooltip="Not published yet">Draft</span>
                        }
                        @if (c.delivers.length) {
                          <span
                            class="delivers tn-small"
                            [class.cross-dp]="c.delivers_cross_dp"
                            [matTooltip]="deliversTooltip(c)"
                          >satisfies {{ deliversLabel(c) }}</span>
                        }
                      </li>

                      <!-- The objective lives with the module that satisfies it, rather
                           than in a parallel list repeating the same 15 relationships. -->
                      @if (objectivesFor(q, c).length) {
                        <li class="course-objectives">
                          <mat-accordion multi displayMode="flat">
                            @for (po of objectivesFor(q, c); track po.po_code) {
                              <ng-container
                                [ngTemplateOutlet]="objectivePanel"
                                [ngTemplateOutletContext]="{ $implicit: po }"
                              />
                            }
                          </mat-accordion>
                        </li>
                      }
                    }
                  </ul>
                </ng-template>
              </mat-expansion-panel>
            }
          </mat-accordion>
        }

        <!-- Objectives this qualification owns but another period teaches. Without
             this they would vanish: the specialty streams are entirely delivered by
             Algonquin Year-3 courses, which are placed on DP1. -->
        @if (deliveredElsewhere(q).length) {
          <h4 class="tn-section-label">Taught in another period</h4>
          <p class="tn-muted tn-small note">
            These belong to this qualification but are delivered by courses taught earlier
            in the path.
          </p>
          <mat-accordion multi displayMode="flat">
            @for (po of deliveredElsewhere(q); track po.po_code) {
              <ng-container
                [ngTemplateOutlet]="objectivePanel"
                [ngTemplateOutletContext]="{ $implicit: po, via: po.delivered_by }"
              />
            }
          </mat-accordion>
        }

        <!-- A gap has to be visible as a gap. An objective with nothing real behind it
             can never show progress, however much courseware exists elsewhere. -->
        @if (undelivered(q).length) {
          <h4 class="tn-section-label tn-gap-count">
            Not yet delivered ({{ undelivered(q).length }})
          </h4>
          <p class="tn-muted tn-small note">
            No course module satisfies these yet, so no learner can make progress against
            them.
          </p>
          <mat-accordion multi displayMode="flat">
            @for (po of undelivered(q); track po.po_code) {
              <ng-container
                [ngTemplateOutlet]="objectivePanel"
                [ngTemplateOutletContext]="{ $implicit: po }"
              />
            }
          </mat-accordion>
        }
      </section>
    }

    <!-- One objective, collapsed to a single row wherever it belongs: under the course
         that delivers it, among those taught in another period, or in the gap list.
         Opening it shows the assessment brief; the enabling objectives fold again. -->
    <ng-template #objectivePanel let-po let-via="via">
      <mat-expansion-panel class="objective" [class.is-current]="po.po_code === currentPoCode">
        <mat-expansion-panel-header>
          <mat-panel-title>
            <span class="po-title">{{ po.title }}</span>
            @if (po.po_code === currentPoCode) {
              <span class="tn-small here">you are here</span>
            }
          </mat-panel-title>
          <mat-panel-description class="tn-small">
            <span class="status-chip" [ngClass]="progressChip(po)">{{ progressLabel(po) }}</span>
            <!-- Labelled "assessed": this is test time, not course length. -->
            @if (po.duration_min) {
              <span>{{ fmtDuration(po.duration_min) }} assessed</span>
            } @else {
              <span
                class="unscoped"
                matTooltip="No duration in the QSP crosswalk — this objective is not scoped yet"
              >no duration</span>
            }
          </mat-panel-description>
        </mat-expansion-panel-header>

        <ng-template matExpansionPanelContent>
            @if (via) {
              <p class="from-where tn-small">
                <span class="tn-muted">Taught via</span>
                <a class="tn-code" [routerLink]="['/learning/courses', via.course_id]">{{ via.course_code }}</a>
                <span class="tn-muted">{{ via.module_title }} · DP{{ via.dp_order }}</span>
              </p>
            }
            <div class="po-top tn-small">
              <span class="tn-code" [title]="po.po_code">{{ po.course_code || po.po_code }}</span>
              <span class="badge" [class.gate]="po.tier === 'gate'">{{ po.tier }}</span>
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
              <mat-accordion displayMode="flat" class="eo-fold">
                <mat-expansion-panel>
                  <mat-expansion-panel-header>
                    <mat-panel-title class="tn-small">
                      {{ po.enabling_objectives.length }}
                      enabling {{ po.enabling_objectives.length === 1 ? 'objective' : 'objectives' }}
                    </mat-panel-title>
                  </mat-expansion-panel-header>
                  <ng-template matExpansionPanelContent>
                    <ul class="eos">
                      @for (eo of po.enabling_objectives; track eo.eo_code) {
                        <li>
                          <span class="eo-code">EO {{ eo.eo_code }}</span> {{ eo.title }}
                          @if (eo.lessons.length) {
                            <span class="lesson-pill">
                              <mat-icon>menu_book</mat-icon>{{ eo.lessons.length }} lesson(s)
                              @if (!anyPublished(eo)) { <em class="draft-note">· draft</em> }
                            </span>
                          }
                        </li>
                      }
                    </ul>
                  </ng-template>
                </mat-expansion-panel>
              </mat-accordion>
            }
        </ng-template>
      </mat-expansion-panel>
    </ng-template>
  `,
  styles: [
    `
      :host { display: block; }

      .qd-meta { margin: 0 0 4px; }
      .note { margin: -2px 0 8px; }

      .course-list { list-style: none; margin: 0; padding: 0; }
      .course {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 4px 12px;
        padding: 7px 0;
        border-bottom: 1px solid var(--border);
      }
      .course:last-child { border-bottom: 0; }
      .course .c-code { min-width: 64px; text-decoration: none; }
      .course .c-code:hover { color: var(--accent); text-decoration: underline; }
      .course .c-name { flex: 1 1 14rem; }
      /* CSS, not the titlecase pipe: the pipe pulls ~8 kB into the initial bundle. */
      .cap { text-transform: capitalize; }
      .delivers { color: var(--text-secondary); cursor: help; }
      .delivers.cross-dp { color: var(--warning); }
      .draft-pill {
        font-size: 0.7rem;
        font-weight: 600;
        padding: 1px 8px;
        border-radius: 999px;
        color: var(--warning);
        border: 1px solid color-mix(in srgb, var(--warning) 45%, transparent);
      }
      /* Objectives a course satisfies sit just under it, indented to its name. */
      .course-objectives { list-style: none; margin: 0 0 6px 76px; }

      .objective .po-title { font-weight: 500; }
      .objective.is-current .po-title { color: var(--accent); }
      .here { color: var(--accent); font-weight: 600; }
      .unscoped { color: var(--warning); cursor: help; }

      .from-where {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 6px;
        margin: 0 0 8px;
      }
      .from-where a { text-decoration: none; }
      .from-where a:hover { color: var(--accent); text-decoration: underline; }

      .po-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .eo-fold { display: block; margin-top: 10px; }
      .draft-note { color: var(--warning); }

      .badge {
        font-size: 0.72rem;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--bg-surface);
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.03em;
      }
      .badge.gate { color: var(--warning); }
      .badge.warn { color: var(--warning); font-weight: 700; }

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
    return undeliveredObjectives(q);
  }

  /** The programme for this qualification, grouped into terms. Shared with the
   * course catalogue so the two views divide a period identically. */
  termsOf(q: QualNode): TermGroup[] {
    return groupByTerm(q.courses);
  }

  /** Taught hours in a term, for its collapsed row. */
  termHours(term: TermGroup): number {
    return term.courses.reduce((sum, c) => sum + (c.duration_hours || 0), 0);
  }

  progressChip(po: PO): string {
    return PROGRESS_CHIP[po.progress_state] ?? PROGRESS_CHIP.not_started;
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
