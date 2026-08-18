import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '@core/services/api.service';
import { CurriculumMap, CurriculumMapService } from '@core/services/curriculum-map.service';
import { CareerMapComponent } from './career-map.component';
import { QualificationDetailComponent } from './qualification-detail.component';

interface LearningPath {
  id: string;
  name: string;
  description: string;
  /** Already a list — `LearningPathOut` parses the stored JSON string server-side. */
  course_ids: string[];
}

/**
 * QSP-derived curriculum: the developmental career map (rank ladder + specialty
 * streams), the selected qualification's objectives, and the generated learning
 * paths. Everything the map needs arrives in one request.
 */
@Component({
  selector: 'tn-qsp-curriculum',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatExpansionModule,
    MatIconModule,
    CareerMapComponent,
    QualificationDetailComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="qc">
      <header class="qc-head">
        <h2><mat-icon>school</mat-icon> Developmental Path</h2>
        <p class="muted">
          The QSP rank ladder and its specialty streams, decomposed into performance
          objectives, enabling objectives and lessons — cross-mapped to NICE work roles
          and NIST CSF 2.0.
        </p>
      </header>

      @if (loading()) {
        <div class="skeleton" aria-live="polite" aria-busy="true">
          <span class="sr-only">Loading the developmental path…</span>
          @for (row of [0, 1, 2]; track row) {
            <div class="sk-lane">
              <div class="sk-label"></div>
              @for (col of [0, 1, 2, 3]; track col) {
                <div class="sk-node"></div>
              }
            </div>
          }
        </div>
      } @else if (error()) {
        <div class="state-panel error" role="alert">
          <mat-icon>error_outline</mat-icon>
          <div>
            <p class="state-title">Could not load the curriculum map</p>
            <p class="muted">{{ error() }}</p>
          </div>
          <button mat-stroked-button (click)="reload()">Retry</button>
        </div>
      } @else if (!map()?.nodes?.length) {
        <div class="state-panel">
          <mat-icon>upload_file</mat-icon>
          <div>
            <p class="state-title">No qualification standards ingested yet</p>
            <p class="muted">
              Import a QSP crosswalk to build the spine, then generate learning paths.
            </p>
          </div>
        </div>
      } @else {
        <tn-career-map
          [map]="map()"
          [selected]="selectedCode()"
          (selectedChange)="select($event)"
        />

        <tn-qualification-detail
          [qualification]="selectedNode()"
          [currentPoCode]="map()?.learner?.current_po_code ?? null"
        />
      }

      @if (paths().length) {
        <mat-accordion class="paths-block">
          <mat-expansion-panel>
            <mat-expansion-panel-header>
              <mat-panel-title>Generated learning paths</mat-panel-title>
              <mat-panel-description>{{ paths().length }} paths</mat-panel-description>
            </mat-expansion-panel-header>
            <div class="paths">
              @for (p of paths(); track p.id) {
                <div class="path-card">
                  <div class="path-name">{{ p.name }}</div>
                  <div class="muted">
                    {{ courseCount(p) }} {{ courseCount(p) === 1 ? 'course' : 'courses' }}
                  </div>
                  <div class="muted small">{{ p.description }}</div>
                </div>
              }
            </div>
          </mat-expansion-panel>
        </mat-accordion>
      }
    </div>
  `,
  styles: [
    `
      .qc { padding: 4px 2px 24px; }
      .qc-head h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 4px; }
      .qc-head p { margin: 0 0 16px; max-width: 76ch; font-size: 0.86rem; }
      .muted { color: var(--text-muted); }
      .small { font-size: 0.8rem; }

      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }

      /* Skeleton mirrors the real grid so the layout does not jump on load. */
      .skeleton { display: flex; flex-direction: column; gap: 12px; }
      .sk-lane { display: grid; grid-template-columns: 140px repeat(4, 1fr); gap: 12px; }
      .sk-label,
      .sk-node {
        height: 96px;
        border-radius: var(--radius-md);
        background: linear-gradient(
          90deg,
          var(--bg-card) 25%,
          var(--bg-surface) 37%,
          var(--bg-card) 63%
        );
        background-size: 400% 100%;
        animation: sk-shimmer 1.4s ease infinite;
      }
      .sk-label { height: 96px; opacity: 0.5; }
      @keyframes sk-shimmer {
        0% { background-position: 100% 50%; }
        100% { background-position: 0 50%; }
      }
      @media (prefers-reduced-motion: reduce) {
        .sk-label,
        .sk-node { animation: none; }
      }

      .state-panel {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px;
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-card);
      }
      .state-panel.error { border-color: var(--alert); }
      .state-panel mat-icon { color: var(--text-muted); }
      .state-panel.error mat-icon { color: var(--alert); }
      .state-panel button { margin-left: auto; }
      .state-title { margin: 0 0 2px; font-weight: 600; }
      .state-panel p { margin: 0; }

      tn-qualification-detail { display: block; margin-top: 22px; }

      .paths-block { display: block; margin-top: 22px; }
      .paths {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
        gap: 12px;
      }
      .path-card {
        padding: 12px 14px;
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--bg-card);
      }
      .path-name { font-weight: 600; }
    `,
  ],
})
export class QspCurriculumComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly curriculum = inject(CurriculumMapService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly map = signal<CurriculumMap | null>(null);
  protected readonly paths = signal<LearningPath[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  /** Selection is held in the URL so a qualification is linkable. */
  protected readonly selectedCode = signal<string | null>(null);

  protected readonly selectedNode = computed(
    () => this.map()?.nodes.find(n => n.qsp_code === this.selectedCode()) ?? null,
  );

  ngOnInit(): void {
    this.selectedCode.set(this.route.snapshot.queryParamMap.get('qual'));
    this.load();

    this.api.get<LearningPath[]>('/learning-paths').subscribe({
      next: p => this.paths.set(p ?? []),
      // Paths are supplementary — the map is still useful without them.
      error: () => this.paths.set([]),
    });
  }

  protected reload(): void {
    this.load(true);
  }

  private load(force = false): void {
    this.loading.set(true);
    this.error.set(null);
    (force ? this.curriculum.refresh() : this.curriculum.map()).subscribe({
      next: data => {
        this.map.set(data);
        this.loading.set(false);
        // Default to wherever the learner is, so the page opens on something useful.
        if (!this.selectedNode()) {
          this.select(data.learner?.current_qsp_code ?? data.nodes[0]?.qsp_code ?? null);
        }
      },
      error: (err: unknown) => {
        this.loading.set(false);
        this.error.set(this.message(err));
      },
    });
  }

  protected select(code: string | null): void {
    this.selectedCode.set(code);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { qual: code },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected courseCount(p: LearningPath): number {
    const ids = p.course_ids;
    if (Array.isArray(ids)) return ids.length;
    // Defensive: older records served the raw JSON string rather than a list.
    try {
      const parsed: unknown = JSON.parse((ids as string) || '[]');
      return Array.isArray(parsed) ? parsed.length : 0;
    } catch {
      return 0;
    }
  }

  private message(err: unknown): string {
    const e = err as { status?: number; error?: { detail?: string }; message?: string };
    if (e?.status === 0) return 'The API is unreachable.';
    return e?.error?.detail ?? e?.message ?? 'Unexpected error.';
  }
}
