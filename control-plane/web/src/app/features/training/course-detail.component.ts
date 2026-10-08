import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';

interface ModuleQuiz {
  id: string;
  title: string;
  pass_pct: number;
  question_count: number;
}

interface OutlineModule {
  id: string;
  ordinal: number;
  title: string;
  content_type: string;
  duration_minutes: number;
  is_required: boolean;
  pass_threshold: number;
  /** Objectives, topics, lab and citations, as markdown. */
  body_markdown: string;
  quiz: ModuleQuiz | null;
  lab: string;
  scenario_id: string | null;
  delivers: {
    qsp_code: string;
    qualification: string;
    po_code: string;
    title: string;
  } | null;
}

interface CourseOutline {
  id: string;
  course_code: string;
  name: string;
  description: string;
  difficulty: string;
  duration_hours: number;
  is_published: boolean;
  institution: string;
  dp_order: number;
  term_label: string;
  provenance: string;
  status: string;
  tags: string[];
  modules: OutlineModule[];
  /** The tenant has a usable Moodle and this course is published to it (any role may read). */
  moodle_available?: boolean;
}

/** One section of a module's teaching content, split out of its markdown. */
interface Section {
  heading: string;
  items: string[];
  prose: string;
}

/**
 * A single course, module by module.
 *
 * The developmental path links here. Before this the only destination was the course
 * *list*, which for a 44-row catalogue meant a link that dropped you next to what you
 * asked for rather than on it.
 */
@Component({
  selector: 'tn-course-detail',
  imports: [RouterLink, MatButtonModule, MatCardModule, MatExpansionModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="cd tn-quiet">
      <a class="back" routerLink="/learning/courses">
        <mat-icon>arrow_back</mat-icon> All courses
      </a>

      @if (loading()) {
        <p class="muted">Loading course…</p>
      } @else if (error()) {
        <mat-card class="err"><mat-card-content>{{ error() }}</mat-card-content></mat-card>
      } @else {
        <!-- An "as" binding is only allowed on a primary @if, never on an @else if. -->
        @if (course(); as c) {
        <header class="head">
          <div>
            <h2>
              @if (c.course_code) { <span class="code">{{ c.course_code }}</span> }
              {{ title(c) }}
            </h2>
            <p class="muted">
              @if (c.institution) { {{ c.institution }} · }
              @if (c.term_label) { {{ c.term_label }} · }
              {{ c.modules.length }} modules
              @if (c.duration_hours) { · {{ c.duration_hours }} h }
              · {{ c.difficulty }}
            </p>
          </div>
          <div class="head-actions">
            <!-- Moodle has no login of its own: the app hands the Student in (local_truenorth).
                 Shown only when the outline says the course is live on the tenant's Moodle. -->
            @if (moodleAvailable()) {
              <button mat-flat-button type="button" (click)="openInMoodle(c)" [disabled]="opening()">
                <mat-icon>open_in_new</mat-icon> Open in Moodle
              </button>
            }
            <!-- Draft state is not decoration: none of this is validated courseware. -->
            <span class="pill" [class.draft]="!c.is_published">
              {{ c.is_published ? 'Published' : 'Draft — not for learners' }}
            </span>
          </div>
        </header>
        @if (moodleError()) { <p class="moodle-err" role="alert">{{ moodleError() }}</p> }

        @if (c.description) { <p class="desc">{{ c.description }}</p> }

        <!-- One row per module; the first required one opens so the page starts on
             content, everything else folds. -->
        <mat-accordion multi displayMode="flat" class="modules">
        @for (m of c.modules; track m.id) {
          <mat-expansion-panel class="mod" [expanded]="m.id === firstRequiredId(c)" [attr.data-module]="m.ordinal">
            <mat-expansion-panel-header>
              <mat-panel-title>
                <span class="ord">{{ m.ordinal }}</span>
                <span class="mod-title">{{ m.title }}</span>
              </mat-panel-title>
              <mat-panel-description class="tn-small">
                @if (m.delivers; as d) {
                  <span
                    class="delivers"
                    [matTooltip]="d.qualification + ' — ' + d.title"
                  >satisfies {{ d.po_code.replace('PO_', 'PO ') }}</span>
                }
                @if (!m.is_required) { <span>optional</span> }
                @if (m.duration_minutes) { <span>{{ m.duration_minutes }} min</span> }
              </mat-panel-description>
            </mat-expansion-panel-header>

            <p class="mod-kind tn-muted tn-small">{{ m.content_type }}</p>

            @for (s of sections(m); track s.heading) {
              <div class="sec">
                <h4>{{ s.heading }}</h4>
                @if (s.items.length) {
                  <ul>
                    @for (i of s.items; track i) { <li>{{ i }}</li> }
                  </ul>
                }
                @if (s.prose) { <p>{{ s.prose }}</p> }
              </div>
            }

            @if (m.quiz; as q) {
              <div class="quiz">
                <mat-icon>quiz</mat-icon>
                <span class="q-title">{{ q.title }}</span>
                <span class="muted">
                  {{ q.question_count }} questions · pass at {{ q.pass_pct }}%
                </span>
              </div>
            }

            @if (m.scenario_id) {
              <div class="quiz">
                <mat-icon>science</mat-icon>
                <span class="q-title">Assessment exercise</span>
                <span class="muted">a range scenario is wired to this module</span>
              </div>
            }
          </mat-expansion-panel>
        } @empty {
          <p class="muted">This course has no modules yet.</p>
        }
        </mat-accordion>
        }
      }
    </div>
  `,
  styles: [
    `
      :host { display: block; }
      .cd { padding: 4px 0 24px; }
      .muted { color: var(--text-muted); }

      .back {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        margin-bottom: 12px;
        font-size: 0.82rem;
        color: var(--text-muted);
        text-decoration: none;
      }
      .back:hover { color: var(--accent); }
      .back mat-icon { font-size: 18px; width: 18px; height: 18px; }

      .head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
      }
      .head h2 { margin: 0 0 2px; font-size: 1.15rem; }
      .head p { margin: 0; font-size: 0.82rem; }
      .code { font-family: var(--font-mono, monospace); color: var(--accent); }

      .head-actions { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
      .moodle-err { margin: 8px 0 0; font-size: 0.82rem; color: var(--warning); }
      .pill {
        flex: 0 0 auto;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 0.7rem;
        font-weight: 700;
        border: 1px solid var(--border-light);
      }
      .pill.draft {
        color: var(--warning);
        border-color: color-mix(in srgb, var(--warning) 45%, transparent);
        background: var(--warn-soft, rgba(180, 110, 0, 0.15));
      }

      .desc { margin: 10px 0 18px; font-size: 0.9rem; max-width: 72ch; color: var(--text-secondary); }
      .err { border-left: 3px solid var(--warning); }

      .cd { max-width: 980px; }
      .modules { display: block; }
      .mod-kind { margin: 0 0 6px; text-transform: capitalize; }
      .delivers { color: var(--text-secondary); cursor: help; }
      .ord {
        flex: 0 0 auto;
        width: 22px;
        height: 22px;
        border-radius: 50%;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 0.7rem;
        font-weight: 700;
        background: var(--bg-secondary);
        border: 1px solid var(--border-light);
      }
      .mod-title { font-weight: 600; }

      .sec { margin: 8px 0; }
      .sec h4 {
        margin: 0 0 3px;
        font-size: 0.7rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
      }
      .sec ul { margin: 0; padding-left: 18px; font-size: 0.84rem; }
      .sec p { margin: 0; font-size: 0.84rem; }

      .quiz {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--border-light);
        font-size: 0.82rem;
      }
      .quiz mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-muted); }
      .q-title { font-weight: 600; }
    `,
  ],
})
export class CourseDetailComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly route = inject(ActivatedRoute);

  protected readonly course = signal<CourseOutline | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly opening = signal(false);
  protected readonly moodleError = signal<string | null>(null);
  /**
   * Whether "Open in Moodle" can work, as the outline reports it for every role: the
   * tenant has an active Moodle with an LTI issuer and this course is published to it
   * (`moodle_sso.course_available`). Missing or false hides the button rather than
   * offering a launch that 404s.
   */
  protected readonly moodleAvailable = computed(() => this.course()?.moodle_available === true);

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (!id) {
      this.error.set('No course id in the URL.');
      this.loading.set(false);
      return;
    }
    this.api.get<CourseOutline>(`/courses/${id}/outline`).subscribe({
      next: c => {
        this.course.set(c);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('That course could not be loaded. It may have been removed.');
        this.loading.set(false);
      },
    });
  }

  /**
   * Sign the Student into this course in their unit's Moodle.
   *
   * The API returns a one-minute, single-use ticket that is POSTed to Moodle, never put
   * in a URL. The tab is opened inside the click so pop-up blockers allow it.
   */
  protected openInMoodle(c: CourseOutline): void {
    const target = 'tn-moodle';
    const tab = window.open('about:blank', target);
    if (tab) tab.opener = null;
    this.opening.set(true);
    this.moodleError.set(null);
    this.api.post<{ action: string; token: string }>('/integrations/moodle/sso', { course_id: c.id }).subscribe({
      next: ({ action, token }) => {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = action;
        form.target = tab ? target : '_self';
        const field = document.createElement('input');
        field.type = 'hidden';
        field.name = 'token';
        field.value = token;
        form.appendChild(field);
        document.body.appendChild(form);
        form.submit();
        form.remove();
        this.opening.set(false);
      },
      error: err => {
        tab?.close();
        this.moodleError.set(err?.error?.detail ?? 'Moodle could not be opened.');
        this.opening.set(false);
      },
    });
  }

  /** The name without its leading code, which is shown separately. */
  protected title(c: CourseOutline): string {
    const sep = c.name.indexOf(' — ');
    return sep === -1 ? c.name : c.name.slice(sep + 3);
  }

  /** The module that opens on load: the first required one, else the first. */
  protected firstRequiredId(c: CourseOutline): string | null {
    return (c.modules.find(m => m.is_required) ?? c.modules[0])?.id ?? null;
  }

  /**
   * Split a module's markdown back into its sections.
   *
   * The body is generated from the course file's own fields, so its shape is known —
   * `## Heading` followed by bullets or prose. Parsing that is cheaper and safer than
   * pulling in a markdown renderer to display content we authored ourselves.
   */
  protected sections(m: OutlineModule): Section[] {
    const out: Section[] = [];
    for (const block of (m.body_markdown || '').split(/\n(?=## )/)) {
      const lines = block.split('\n');
      const heading = (lines.shift() || '').replace(/^##\s*/, '').trim();
      if (!heading) continue;
      const items: string[] = [];
      const prose: string[] = [];
      for (const line of lines) {
        const t = line.trim();
        if (!t) continue;
        if (t.startsWith('- ')) items.push(t.slice(2));
        else prose.push(t);
      }
      out.push({ heading, items, prose: prose.join(' ') });
    }
    return out;
  }
}
