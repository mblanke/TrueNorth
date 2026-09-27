import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import {
  CurriculumMap,
  CurriculumMapService,
  DPStage,
  NodeCourse,
  TermGroup,
  groupByTerm,
} from '@core/services/curriculum-map.service';
import { EmptyStateComponent } from '@shared/components/empty-state/empty-state.component';

export interface CatalogueStage {
  stage: DPStage;
  terms: TermGroup[];
  count: number;
}

/**
 * Group the catalogue by the period that teaches each course, then by term.
 *
 * The courses already ride on the curriculum map (the career path loads it and the
 * service caches it), carrying code, term and hours — which the paginated
 * `GET /courses` list does not. A course can sit under more than one qualification in
 * the same period (the ladder and a specialty stream), so it is listed once per period.
 */
export function catalogueStages(map: CurriculumMap | null, query = ''): CatalogueStage[] {
  if (!map) return [];
  const q = query.trim().toLowerCase();
  const matches = (c: NodeCourse) =>
    !q || [c.course_code, c.name, c.term_label, c.institution].some(s => (s || '').toLowerCase().includes(q));

  const out: CatalogueStage[] = [];
  for (const stage of [...map.stages].sort((a, b) => a.dp_order - b.dp_order)) {
    const seen = new Set<string>();
    const courses: NodeCourse[] = [];
    for (const node of map.nodes.filter(n => n.dp_order === stage.dp_order)) {
      for (const c of node.courses) {
        if (seen.has(c.course_id) || !matches(c)) continue;
        seen.add(c.course_id);
        courses.push(c);
      }
    }
    if (courses.length) out.push({ stage, terms: groupByTerm(courses), count: courses.length });
  }
  return out;
}

/**
 * The course catalogue as a learner sees it: read-only, grouped by period and term,
 * every group collapsible. One quiet row per course, linking to the course page.
 * Administration (create, edit, enrolments) lives on the Admin tab.
 */
@Component({
  selector: 'tn-course-catalogue',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatButtonModule,
    MatExpansionModule,
    MatIconModule,
    MatTooltipModule,
    EmptyStateComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="cat tn-quiet">
      <header class="cat-head">
        <p class="tn-muted">Every course in the programme, by the period and term that teaches it.</p>
        <label class="search">
          <mat-icon aria-hidden="true">search</mat-icon>
          <span class="sr-only">Search courses</span>
          <input
            type="search"
            placeholder="Search by code, name or term"
            [value]="query()"
            (input)="query.set($any($event.target).value)"
          />
        </label>
      </header>

      @if (loading()) {
        <p class="tn-muted" aria-live="polite">Loading the catalogue…</p>
      } @else if (error()) {
        <tn-empty-state icon="error_outline" title="Could not load the catalogue" [message]="error()!">
          <button mat-stroked-button type="button" (click)="load(true)">Retry</button>
        </tn-empty-state>
      } @else if (!stages().length) {
        @if (query()) {
          <tn-empty-state icon="search_off" title="No courses match" [message]="'Nothing matches “' + query() + '”.'">
            <button mat-button type="button" (click)="query.set('')">Clear search</button>
          </tn-empty-state>
        } @else {
          <tn-empty-state
            icon="menu_book"
            title="No courses in the catalogue yet"
            message="Courses appear here once a programme has been imported."
          />
        }
      } @else {
        <mat-accordion multi displayMode="flat">
          @for (s of stages(); track s.stage.dp_order) {
            <mat-expansion-panel
              class="stage"
              [attr.data-stage]="s.stage.dp_order"
              [expanded]="isOpen(s.stage.dp_order)"
              (opened)="setOpen(s.stage.dp_order, true)"
              (closed)="setOpen(s.stage.dp_order, false)"
            >
              <mat-expansion-panel-header>
                <mat-panel-title>
                  <span class="kicker tn-small">
                    DP {{ s.stage.dp_order }}@if (s.stage.rank_level) { · {{ s.stage.rank_level }} }
                  </span>
                  <span>{{ s.stage.label }}</span>
                </mat-panel-title>
                <mat-panel-description class="tn-small">
                  {{ s.count }} {{ s.count === 1 ? 'course' : 'courses' }}
                </mat-panel-description>
              </mat-expansion-panel-header>

              <ng-template matExpansionPanelContent>
                <mat-accordion multi displayMode="flat">
                  @for (t of s.terms; track t.code) {
                    <mat-expansion-panel class="term" [expanded]="!!query() || s.terms.length === 1">
                      <mat-expansion-panel-header>
                        <mat-panel-title>{{ t.label || t.code }}</mat-panel-title>
                        <mat-panel-description class="tn-small">
                          {{ t.courses.length }} {{ t.courses.length === 1 ? 'course' : 'courses' }}
                        </mat-panel-description>
                      </mat-expansion-panel-header>
                      <ul class="courses">
                        @for (c of t.courses; track c.course_id) {
                          <li>
                            <a class="course" [routerLink]="['/learning/courses', c.course_id]" [attr.data-course]="c.course_code">
                              <span class="tn-code">{{ c.course_code }}</span>
                              <span class="name">{{ title(c) }}</span>
                              <span class="meta tn-muted tn-small">
                                <span class="cap">{{ c.difficulty }}</span>@if (c.duration_hours) { · {{ c.duration_hours }} h }
                              </span>
                              @if (!c.is_published) {
                                <span class="draft" matTooltip="Not published yet — content may change">Draft</span>
                              }
                            </a>
                          </li>
                        }
                      </ul>
                    </mat-expansion-panel>
                  }
                </mat-accordion>
              </ng-template>
            </mat-expansion-panel>
          }
        </mat-accordion>
      }
    </div>
  `,
  styles: [
    `
      .cat { padding: 4px 2px 24px; max-width: 980px; }
      .cat-head {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin: 0 0 16px;
      }
      .cat-head p { margin: 0; font-size: 0.9rem; }
      .search {
        display: flex;
        align-items: center;
        gap: 6px;
        flex: 0 1 300px;
        padding: 6px 12px;
        border: 1px solid var(--border);
        border-radius: 999px;
        background: var(--bg-input);
        color: var(--text-muted);
      }
      .search:focus-within { border-color: var(--accent); }
      .search input {
        flex: 1 1 auto;
        min-width: 0;
        border: 0;
        outline: 0;
        background: transparent;
        color: var(--text-primary);
        font: inherit;
        font-size: 0.9rem;
      }
      .search mat-icon { font-size: 18px; width: 18px; height: 18px; }

      .kicker { min-width: 7.5rem; color: var(--text-muted); font-weight: 500; }

      .courses { list-style: none; margin: 0; padding: 0; }
      .course {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 4px 12px;
        padding: 8px 6px;
        border-bottom: 1px solid var(--border);
        color: inherit;
        text-decoration: none;
        border-radius: var(--radius-sm);
      }
      .courses li:last-child .course { border-bottom: 0; }
      .course:hover { background: color-mix(in srgb, var(--text-primary) 4%, transparent); }
      .course:hover .name { color: var(--accent); }
      .course:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
      .course .tn-code { min-width: 64px; }
      .name { flex: 1 1 14rem; }
      /* CSS, not the titlecase pipe: the pipe pulls ~8 kB into the initial bundle. */
      .cap { text-transform: capitalize; }
      .draft {
        font-size: 0.7rem;
        font-weight: 600;
        padding: 1px 8px;
        border-radius: 999px;
        color: var(--warning);
        border: 1px solid color-mix(in srgb, var(--warning) 45%, transparent);
      }

      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }

      @media (max-width: 640px) {
        .kicker { min-width: 0; }
        .search { flex: 1 1 100%; }
      }
    `,
  ],
})
export class CourseCatalogueComponent implements OnInit {
  private readonly curriculum = inject(CurriculumMapService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly map = signal<CurriculumMap | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly query = signal('');
  /** Stages opened by hand. While searching, every matching stage is open instead. */
  private readonly opened = signal<Set<number>>(new Set());

  protected readonly stages = computed(() => catalogueStages(this.map(), this.query()));

  ngOnInit(): void {
    // `/training?course=<id>` (an LTI course launch, or an old link) lands here via the
    // redirect. The course has its own page now, so go straight to it.
    const linked = this.route.snapshot.queryParamMap.get('course');
    if (linked) {
      this.router.navigate(['/learning/courses', linked], { replaceUrl: true });
      return;
    }
    this.load();
  }

  load(force = false): void {
    this.loading.set(true);
    this.error.set(null);
    (force ? this.curriculum.refresh() : this.curriculum.map()).subscribe({
      next: data => {
        this.map.set(data);
        this.loading.set(false);
        // Open on the learner's own period, so the page starts on something useful.
        const current = data.nodes.find(n => n.qsp_code === data.learner?.current_qsp_code);
        this.opened.set(new Set(current ? [current.dp_order] : []));
      },
      error: (err: { status?: number; error?: { detail?: string }; message?: string }) => {
        this.loading.set(false);
        this.error.set(err?.status === 0 ? 'The API is unreachable.' : err?.error?.detail ?? err?.message ?? 'Unexpected error.');
      },
    });
  }

  protected isOpen(dp: number): boolean {
    return !!this.query().trim() || this.opened().has(dp);
  }

  protected setOpen(dp: number, open: boolean): void {
    if (this.query().trim()) return; // search controls expansion; don't record it
    const next = new Set(this.opened());
    if (open) next.add(dp);
    else next.delete(dp);
    this.opened.set(next);
  }

  /** The course name without its leading code, which is shown separately. */
  protected title(c: NodeCourse): string {
    const sep = c.name.indexOf(' — ');
    return sep === -1 ? c.name : c.name.slice(sep + 3);
  }
}
