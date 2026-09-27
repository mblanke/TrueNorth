import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { ApiService } from '@core/services/api.service';
import { CurriculumMap, CurriculumMapService } from '@core/services/curriculum-map.service';
import { CareerPathListComponent } from './career-path-list.component';
import { CareerMapComponent } from './career-map.component';

interface LearningPath {
  id: string;
  name: string;
  description: string;
  /** Already a list — `LearningPathOut` parses the stored JSON string server-side. */
  course_ids: string[];
}

/**
 * QSP-derived curriculum: the developmental path (rank ladder + specialty streams) as
 * a collapsible list, each stage opening onto its programme and objectives, plus the
 * generated learning paths. Everything the path needs arrives in one request.
 */
@Component({
  selector: 'tn-qsp-curriculum',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatExpansionModule,
    MatIconModule,
    MatTooltipModule,
    CareerPathListComponent,
    CareerMapComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="qc tn-quiet">
      <!-- The hub already titles the page; one quiet line says what this tab is. -->
      <header class="qc-head">
        <p class="tn-muted">
          Your developmental path: each stage of the rank ladder and its specialty streams,
          with the courses that teach it and the objectives you are assessed on.
        </p>
        <!-- The map is cached for the lifetime of the page (a root-scoped
             shareReplay), so an import lands invisibly until something drops it.
             Without a control here the only way to refetch was a browser reload. -->
        <button
          mat-button
          type="button"
          class="refresh"
          (click)="reload()"
          [disabled]="loading()"
          matTooltip="Refetch after a crosswalk import, a content import or path generation"
        >
          <mat-icon>refresh</mat-icon> Refresh
        </button>
      </header>

      @if (loading()) {
        <div class="skeleton" aria-live="polite" aria-busy="true">
          <span class="sr-only">Loading the developmental path…</span>
          @for (row of [0, 1, 2, 3, 4]; track row) {
            <div class="sk-row"></div>
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
        <!-- The visual career-map tile: rank-ladder stages with each period's
             term track, clickable course chips and prerequisite arrows. -->
        <tn-career-map
          [map]="map()"
          [selected]="selectedCode()"
          (selectedChange)="select($event)"
        />

        <!-- The selected stage's objectives + programme, folded, below the map. -->
        <tn-career-path-list
          [map]="map()"
          [selected]="selectedCode()"
          [currentPoCode]="map()?.learner?.current_po_code ?? null"
          (selectedChange)="select($event)"
        />
      }

      @if (paths().length) {
        <mat-accordion class="paths-block" displayMode="flat">
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
      .qc { padding: 4px 2px 24px; max-width: 980px; }
      .qc-head {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-start;
        justify-content: space-between;
        gap: 4px 12px;
        margin: 0 0 16px;
      }
      .qc-head p { flex: 1 1 18rem; }
      .qc-head p { margin: 4px 0 0; max-width: 72ch; font-size: 0.9rem; }
      .refresh { flex: 0 0 auto; color: var(--text-muted); }
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

      /* Skeleton mirrors the real list so the layout does not jump on load. */
      .skeleton { display: flex; flex-direction: column; gap: 1px; }
      .sk-row {
        height: 52px;
        background: linear-gradient(
          90deg,
          var(--bg-card) 25%,
          var(--bg-surface) 37%,
          var(--bg-card) 63%
        );
        background-size: 400% 100%;
        animation: sk-shimmer 1.4s ease infinite;
      }
      .sk-row:first-child { border-radius: var(--radius-md) var(--radius-md) 0 0; }
      .sk-row:last-child { border-radius: 0 0 var(--radius-md) var(--radius-md); }
      @keyframes sk-shimmer {
        0% { background-position: 100% 50%; }
        100% { background-position: 0 50%; }
      }
      @media (prefers-reduced-motion: reduce) {
        .sk-row { animation: none; }
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
        // The map above is the navigator and marks the learner's current stage on its
        // own ("you are here"), so the detail list below starts collapsed. It opens when
        // a stage is picked, or from a `?qual=` deep link — rather than dumping the
        // current stage's whole programme and objectives on load.
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
