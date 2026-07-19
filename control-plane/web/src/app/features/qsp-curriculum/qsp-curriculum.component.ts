import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { environment } from '@env/environment';

interface Lesson {
  title: string;
  is_published: boolean;
}
interface EO {
  eo_code: string;
  title: string;
  lessons: Lesson[];
}
interface Comp {
  framework: string;
  code: string;
  name: string;
  relation: string;
}
interface PO {
  po_code: string;
  title: string;
  tier: string;
  environment: string;
  status: string;
  duration_min: number;
  critical_events: string[];
  nice_dcwf_task: string;
  enabling_objectives: EO[];
  competencies: Comp[];
  exercise_id?: string | null;
  course_code?: string | null;
  duration_long?: boolean;
}
interface Qual {
  id: string;
  qsp_code: string;
  nqual: string;
  title: string;
  po_count: number;
  objectives?: PO[];
}
interface DPEntry {
  qsp_code: string;
  nqual: string;
  title: string;
  dp_order: number;
  rank_level: string;
  po_count: number;
}
interface LearningPath {
  id: string;
  name: string;
  description: string;
  course_ids: string;
}

/** QSP-derived curriculum: developmental progression, qualifications -> POs -> EOs/lessons,
 *  NICE + NIST CSF tags, and the generated learning paths. */
@Component({
  selector: 'tn-qsp-curriculum',
  standalone: true,
  imports: [
    CommonModule,
    RouterLink,
    MatCardModule,
    MatChipsModule,
    MatExpansionModule,
    MatIconModule,
    MatProgressBarModule,
  ],
  template: `
    <div class="qc">
      <header class="qc-head">
        <h2><mat-icon>school</mat-icon> QSP Curriculum &amp; Learning Plans</h2>
        <p class="muted">
          Qualification standards decomposed into performance objectives, enabling objectives and
          lessons — cross-mapped to NICE work roles and NIST CSF 2.0.
        </p>
      </header>

      @if (loading) {
        <mat-progress-bar mode="indeterminate" />
      }

      <!-- Developmental progression -->
      @if (progression.length || specialties.length) {
        <section class="block">
          <h3>Developmental Progression</h3>
          <div class="dp-row">
            @for (dp of progression; track dp.qsp_code) {
              <mat-card class="dp-card">
                <div class="dp-order">DP {{ dp.dp_order }} · {{ dp.rank_level }}</div>
                <div class="dp-title">{{ dp.title }}</div>
                <div class="muted">{{ dp.qsp_code }} · {{ dp.po_count }} POs</div>
              </mat-card>
              @if (!$last) {
                <mat-icon class="dp-arrow">arrow_forward</mat-icon>
              }
            }
          </div>
          @if (specialties.length) {
            <div class="dp-row spec">
              <span class="spec-label">Specialty streams:</span>
              @for (s of specialties; track s.qsp_code) {
                <mat-chip-set><mat-chip>{{ s.title }} ({{ s.po_count }})</mat-chip></mat-chip-set>
              }
            </div>
          }
        </section>
      }

      <!-- Qualifications -> POs -> EOs -->
      <section class="block">
        <h3>Qualifications</h3>
        <mat-accordion multi>
          @for (q of quals; track q.qsp_code) {
            <mat-expansion-panel>
              <mat-expansion-panel-header>
                <mat-panel-title>{{ q.title || q.nqual }}</mat-panel-title>
                <mat-panel-description>{{ q.qsp_code }} · {{ q.po_count }} POs</mat-panel-description>
              </mat-expansion-panel-header>

              @for (po of q.objectives; track po.po_code) {
                <mat-card class="po-card">
                  <div class="po-top">
                    <span class="po-code" [title]="po.po_code">{{ po.course_code || po.po_code }}</span>
                    <span class="po-title">{{ po.title }}</span>
                    <span class="badge" [class.gate]="po.tier === 'gate'">{{ po.tier }}</span>
                    <span class="badge env">{{ po.environment }}</span>
                    <span class="badge dur" *ngIf="po.duration_min">{{ fmtDuration(po.duration_min) }}</span>
                    <span class="badge warn" *ngIf="po.duration_long"
                          title="Unusually long assessment (8h+) — verify the duration is intentional">⚠ long</span>
                    <a
                      *ngIf="po.exercise_id"
                      class="ex-link"
                      [routerLink]="['/exercises', po.exercise_id]"
                    >Open exercise <mat-icon>chevron_right</mat-icon></a>
                  </div>

                  <div class="tags">
                    @for (c of nice(po); track c.code) {
                      <span class="tag nice" title="NICE work role">{{ c.name }} ({{ c.code }})</span>
                    }
                    @for (c of nist(po); track c.code) {
                      <span class="tag nist" title="NIST CSF 2.0">{{ c.code }}</span>
                    }
                  </div>

                  @if (po.critical_events?.length) {
                    <div class="crit"><strong>Critical events:</strong> {{ po.critical_events.join(', ') }}</div>
                  }

                  @if (po.enabling_objectives?.length) {
                    <ul class="eos">
                      @for (eo of po.enabling_objectives; track eo.eo_code) {
                        <li>
                          <span class="eo-code">EO {{ eo.eo_code }}</span> {{ eo.title }}
                          @if (eo.lessons?.length) {
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
              }
              @if (q.objectives && q.objectives.length === 0) {
                <p class="muted">No performance objectives.</p>
              }
            </mat-expansion-panel>
          }
        </mat-accordion>
      </section>

      <!-- Learning paths -->
      @if (paths.length) {
        <section class="block">
          <h3>Learning Paths <span class="muted">({{ paths.length }})</span></h3>
          <div class="paths">
            @for (p of paths; track p.id) {
              <mat-card class="path-card">
                <div class="path-name">{{ p.name }}</div>
                <div class="muted">{{ courseCount(p) }} courses</div>
                <div class="muted small">{{ p.description }}</div>
              </mat-card>
            }
          </div>
        </section>
      }
    </div>
  `,
  styles: [
    `
      .qc { padding: 4px 2px 24px; }
      .qc-head h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; }
      .muted { color: var(--text-muted, #8a94a6); }
      .small { font-size: 0.8rem; }
      .block { margin-top: 20px; }
      .block h3 { margin: 0 0 10px; font-size: 1.05rem; }
      .dp-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .dp-row.spec { margin-top: 12px; }
      .spec-label { font-weight: 600; }
      .dp-card { padding: 12px 14px; min-width: 220px; }
      .dp-order { font-size: 0.75rem; letter-spacing: .04em; text-transform: uppercase; color: var(--accent, #5b9bd5); }
      .dp-title { font-weight: 600; margin: 2px 0; }
      .dp-arrow { color: var(--text-muted, #8a94a6); }
      .po-card { padding: 12px 14px; margin: 10px 0; }
      .po-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .po-code { font-weight: 700; }
      .po-title { flex: 1 1 auto; }
      .badge { font-size: 0.72rem; padding: 2px 8px; border-radius: 10px; background: rgba(120,140,170,.18); text-transform: uppercase; letter-spacing: .03em; }
      .badge.gate { background: rgba(214,158,46,.22); }
      .badge.env { background: rgba(91,155,213,.18); }
      .badge.dur { background: transparent; color: var(--text-muted,#8a94a6); }
      .badge.warn { background: rgba(214,158,46,.22); color: #d69e2e; font-weight: 700; }
      .ex-link { display: inline-flex; align-items: center; gap: 2px; margin-left: auto; font-size: 0.78rem; color: var(--accent,#5b9bd5); text-decoration: none; font-weight: 600; }
      .ex-link mat-icon { font-size: 16px; width: 16px; height: 16px; }
      .tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
      .tag { font-size: 0.74rem; padding: 2px 9px; border-radius: 12px; border: 1px solid transparent; }
      .tag.nice { background: rgba(72,187,120,.14); border-color: rgba(72,187,120,.4); }
      .tag.nist { background: rgba(129,140,248,.14); border-color: rgba(129,140,248,.4); font-weight: 600; }
      .crit { font-size: 0.85rem; margin: 4px 0; }
      .eos { margin: 8px 0 0; padding-left: 18px; }
      .eos li { margin: 3px 0; }
      .eo-code { font-weight: 600; }
      .lesson-pill { display: inline-flex; align-items: center; gap: 3px; margin-left: 8px; font-size: 0.75rem; color: var(--text-muted,#8a94a6); }
      .lesson-pill mat-icon { font-size: 15px; width: 15px; height: 15px; }
      .paths { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
      .path-card { padding: 12px 14px; }
      .path-name { font-weight: 600; }
    `,
  ],
})
export class QspCurriculumComponent implements OnInit {
  private http = inject(HttpClient);
  private base = environment.apiUrl;

  loading = true;
  progression: DPEntry[] = [];
  specialties: DPEntry[] = [];
  quals: Qual[] = [];
  paths: LearningPath[] = [];

  ngOnInit(): void {
    this.http.get<{ progression: DPEntry[]; specialty_streams: DPEntry[] }>(
      `${this.base}/qsp/developmental-progression`,
    ).subscribe({
      next: d => {
        this.progression = d.progression ?? [];
        this.specialties = d.specialty_streams ?? [];
      },
      error: () => {},
    });

    this.http.get<Qual[]>(`${this.base}/qsp/qualifications`).subscribe({
      next: qs => {
        this.quals = qs ?? [];
        this.loading = false;
        for (const q of this.quals) {
          this.http.get<PO[]>(`${this.base}/qsp/qualifications/${q.qsp_code}/objectives`).subscribe({
            next: pos => (q.objectives = pos ?? []),
            error: () => (q.objectives = []),
          });
        }
      },
      error: () => (this.loading = false),
    });

    this.http.get<LearningPath[]>(`${this.base}/learning-paths`).subscribe({
      next: p => (this.paths = p ?? []),
      error: () => {},
    });
  }

  /** Minutes -> human-readable ("8 h", "13 h", "1 day (24 h)"). */
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

  nice(po: PO): Comp[] {
    return (po.competencies ?? []).filter(c => c.framework === 'nice' && c.relation === 'primary');
  }
  nist(po: PO): Comp[] {
    return (po.competencies ?? []).filter(c => c.framework === 'nist_csf' && c.relation === 'primary');
  }
  anyPublished(eo: EO): boolean {
    return (eo.lessons ?? []).some(l => l.is_published);
  }
  courseCount(p: LearningPath): number {
    try {
      return JSON.parse(p.course_ids || '[]').length;
    } catch {
      return 0;
    }
  }
}
