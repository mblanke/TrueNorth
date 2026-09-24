import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatTooltipModule } from '@angular/material/tooltip';
import { NodeCourse, PO, POProgressState, QualNode } from '@core/services/curriculum-map.service';
import { undeliveredObjectives } from './qualification-detail.component';

const PROGRESS_LABEL: Record<POProgressState, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  completed: 'Complete',
  failed: 'Not met',
};

const PROGRESS_CHIP: Record<POProgressState, string> = {
  not_started: 'pending',
  in_progress: 'active',
  completed: 'completed',
  failed: 'failed',
};

/**
 * A brief recap of a qualification, shown inside its open row under the career-map.
 *
 * The map tile above already carries the visual (stages, terms, course chips,
 * prerequisite arrows), so this is deliberately short: one line per course, one line
 * per objective. No assessment briefs, standards or enabling-objective folds — that
 * detail lives in the QSP and is not repeated here.
 */
@Component({
  selector: 'tn-qual-summary',
  standalone: true,
  imports: [CommonModule, RouterLink, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (qualification; as q) {
      <section class="qs">
        <p class="tn-muted tn-small qs-meta">
          @if (institution(q)) { {{ institution(q) }} · }
          {{ q.po_count }} objectives
          @if (q.course_count) {
            · {{ q.course_count }} courses
            @if (q.course_hours) { · {{ q.course_hours.toLocaleString() }} taught hours }
          }
          @if (q.total_minutes) { · {{ fmtHours(q.total_minutes) }} assessed }
        </p>

        @if (q.courses.length) {
          <h4 class="tn-section-label">Courses</h4>
          <ul class="course-list">
            @for (c of q.courses; track c.course_id) {
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
              </li>
            }
          </ul>
        }

        @if (q.objectives.length) {
          <h4 class="tn-section-label">Objectives</h4>
          <ul class="obj-list">
            @for (po of q.objectives; track po.po_code) {
              <li class="obj" [class.is-current]="po.po_code === currentPoCode">
                <span class="po-title">{{ po.title }}</span>
                @if (po.po_code === currentPoCode) { <span class="tn-small here">you are here</span> }
                <span class="status-chip" [ngClass]="chip(po)">{{ label(po) }}</span>
              </li>
            }
          </ul>
          @if (gapCount(q)) {
            <p class="tn-muted tn-small tn-gap-count note">
              {{ gapCount(q) }} not yet delivered — no course satisfies these yet.
            </p>
          }
        }
      </section>
    }
  `,
  styles: [
    `
      :host { display: block; }
      .qs { padding: 2px 0 4px; }
      .qs-meta { margin: 0 0 6px; }
      .note { margin: 6px 0 0; }

      .course-list,
      .obj-list { list-style: none; margin: 0; padding: 0; }

      .course,
      .obj {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 4px 12px;
        padding: 7px 0;
        border-bottom: 1px solid var(--border);
      }
      .course:last-child,
      .obj:last-child { border-bottom: 0; }

      .c-code { min-width: 64px; text-decoration: none; }
      .c-code:hover { color: var(--accent); text-decoration: underline; }
      .c-name { flex: 1 1 14rem; }
      .cap { text-transform: capitalize; }

      .draft-pill {
        font-size: 0.7rem;
        font-weight: 600;
        padding: 1px 8px;
        border-radius: 999px;
        color: var(--warning);
        border: 1px solid color-mix(in srgb, var(--warning) 45%, transparent);
      }

      .obj .po-title { flex: 1 1 14rem; font-weight: 500; }
      .obj.is-current .po-title { color: var(--accent); }
      .here { color: var(--accent); font-weight: 600; }
      .obj .status-chip { flex: 0 0 auto; }
    `,
  ],
})
export class QualSummaryComponent {
  @Input({ required: true }) qualification!: QualNode | null;
  @Input() currentPoCode: string | null = null;

  protected institution(q: QualNode): string {
    return q.courses[0]?.institution ?? '';
  }

  /** Course name with any leading "CODE — " stripped, so the code isn't shown twice. */
  protected courseTitle(c: NodeCourse): string {
    const prefix = `${c.course_code} — `;
    return c.name.startsWith(prefix) ? c.name.slice(prefix.length) : c.name;
  }

  protected fmtHours(minutes: number): string {
    const h = Math.round((minutes / 60) * 10) / 10;
    return `${Number.isInteger(h) ? h : h.toFixed(1)} h`;
  }

  protected chip(po: PO): string {
    return PROGRESS_CHIP[po.progress_state] ?? 'pending';
  }

  protected label(po: PO): string {
    return PROGRESS_LABEL[po.progress_state] ?? po.progress_state;
  }

  protected gapCount(q: QualNode): number {
    return undeliveredObjectives(q).length;
  }
}
