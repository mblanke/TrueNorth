import { AfterViewInit, Component, ElementRef, NgZone, OnDestroy, OnInit, ViewChild, signal, inject } from '@angular/core';
import { ActivatedRoute, RouterModule } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';

import { ApiService } from '@core/services/api.service';
import { RangeDiagram, RangeSummary } from '@core/models';
import { MotionService } from '../../shared/motion';
import { HoverInfo, TopologyScene, parseDiagram } from './topology-scene';

@Component({
  selector: 'tn-topology-3d',
  imports: [
    RouterModule,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatIconModule,
    MatSelectModule,
    MatTooltipModule
],
  template: `
    <div class="page-container topo-page">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">3d_rotation</mat-icon>
          <div>
            <div class="tn-kicker">Range Topology</div>
            <h1>3D Topology Viewer</h1>
            <p class="subtitle">Orbit, zoom, and inspect the deployed network — read-only</p>
          </div>
        </div>
        <div class="header-actions">
          <mat-form-field appearance="outline" subscriptSizing="dynamic">
            <mat-label>Range</mat-label>
            <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedRangeId" (selectionChange)="loadDiagram()">
              @for (r of ranges(); track r.id) {
                <mat-option [value]="r.id">{{ r.name }}</mat-option>
              }
            </mat-select>
          </mat-form-field>
          <button mat-stroked-button [routerLink]="['/authoring/ranges/designer']" [queryParams]="{ range: selectedRangeId }"
                  [disabled]="!selectedRangeId" matTooltip="Open this range in the 2D designer">
            <mat-icon>architecture</mat-icon> 2D Designer
          </button>
        </div>
      </div>

      <div class="canvas-wrap">
        <canvas #canvas></canvas>

        @if (loading()) {
          <div class="overlay-message">
            <mat-icon class="spin">sync</mat-icon> Loading topology…
          </div>
        }
        @if (!loading() && failed()) {
          <div class="overlay-message" role="alert">
            <mat-icon>error_outline</mat-icon>
            <p>Could not load this range's topology.</p>
            <p class="hint">Check the connection and pick the range again.</p>
          </div>
        } @else if (!loading() && empty()) {
          <div class="overlay-message" role="status">
            <mat-icon>category</mat-icon>
            <p>Nothing to draw for this range.</p>
            <p class="hint">{{ notes()[0] || 'It has no saved diagram and its template declares no hosts.' }}</p>
          </div>
        }
        @if (!loading() && generated()) {
          <div class="source-chip" role="note" [matTooltip]="notes().join(' ')">
            <mat-icon inline>auto_awesome</mat-icon> Drawn from the template · not a saved design
          </div>
        }

        @if (hover(); as h) {
          <div class="node-tooltip" [style.left.px]="h.clientX + 14" [style.top.px]="h.clientY + 14">
            <div class="tip-title">{{ h.node.label }}</div>
            <div class="tip-row"><span>Type</span>{{ h.node.type }}</div>
            @if (h.node.data['ip']) { <div class="tip-row"><span>IP</span>{{ h.node.data['ip'] }}</div> }
            @if (h.node.data['os']) { <div class="tip-row"><span>OS</span>{{ h.node.data['os'] }}</div> }
            @if (h.node.data['vcpu']) { <div class="tip-row"><span>vCPU</span>{{ h.node.data['vcpu'] }}</div> }
            @if (h.node.data['ram_mb']) { <div class="tip-row"><span>RAM</span>{{ h.node.data['ram_mb'] }} MB</div> }
            @if (h.node.data['status']) { <div class="tip-row"><span>Status</span>{{ h.node.data['status'] }}</div> }
          </div>
        }
      </div>
    </div>
  `,
  styles: [`
    .topo-page { display: flex; flex-direction: column; height: calc(100vh - 56px); max-width: none; }
    .header-actions { display: flex; gap: 12px; align-items: center; }
    .subtitle { color: var(--text-muted); }

    .canvas-wrap {
      position: relative;
      flex: 1;
      min-height: 420px;
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-md);
      overflow: hidden;
      background: var(--bg-primary);
    }
    .canvas-wrap canvas { display: block; width: 100%; height: 100%; }

    .overlay-message {
      position: absolute; inset: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 8px; color: var(--text-muted); pointer-events: none; text-align: center;
    }
    .overlay-message mat-icon { font-size: 42px; width: 42px; height: 42px; color: var(--border-light); }
    .overlay-message .hint { font-size: 12px; }
    .source-chip {
      position: absolute; top: 12px; left: 12px; display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
      border: 1px solid var(--accent); color: var(--accent); background: var(--glass-bg);
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spin { animation: spin 1.2s linear infinite; }

    .node-tooltip {
      position: fixed;
      z-index: 50;
      min-width: 160px;
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--glass-border);
      background: var(--glass-bg);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow-2);
      pointer-events: none;
    }
    .tip-title { font-family: var(--font-display); font-weight: 700; margin-bottom: 6px; color: var(--text-primary); }
    .tip-row { display: flex; justify-content: space-between; gap: 16px; font-size: 12px; color: var(--text-secondary); }
    .tip-row span { color: var(--text-muted); text-transform: uppercase; font-size: 10px; letter-spacing: 0.8px; }
  `],
})
export class Topology3dComponent implements OnInit, AfterViewInit, OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private motion = inject(MotionService);
  private zone = inject(NgZone);

  @ViewChild('canvas') canvasRef!: ElementRef<HTMLCanvasElement>;

  ranges = signal<RangeSummary[]>([]);
  loading = signal(false);
  empty = signal(false);
  failed = signal(false);
  /** Drawn by the API from the range's template (no saved designer diagram). */
  generated = signal(false);
  notes = signal<string[]>([]);
  hover = signal<HoverInfo | null>(null);

  selectedRangeId = '';

  private scene?: TopologyScene;
  private viewReady = false;
  private destroyed = false;

  ngOnInit(): void {
    this.selectedRangeId = this.route.snapshot.queryParamMap.get('range') ?? '';
    this.api.listRanges().subscribe({
      next: r => {
        this.ranges.set(r);
        if (!this.selectedRangeId && r.length) {
          this.selectedRangeId = r[0].id;
          if (this.viewReady) this.loadDiagram();
        }
      },
      error: () => this.failed.set(true),
    });
  }

  ngAfterViewInit(): void {
    this.viewReady = true;
    if (this.selectedRangeId) {
      this.loadDiagram();
    }
  }

  loadDiagram(): void {
    if (!this.selectedRangeId) return;
    this.teardownScene();
    this.loading.set(true);
    this.empty.set(false);
    this.failed.set(false);
    this.generated.set(false);
    this.notes.set([]);
    this.api.getRangeDiagram(this.selectedRangeId).subscribe({
      next: (res: RangeDiagram) => {
        this.generated.set(!!res.generated);
        this.notes.set(res.notes ?? []);
        this.buildScene(res.diagram_json);
      },
      error: () => {
        this.loading.set(false);
        this.failed.set(true);
      },
    });
  }

  private buildScene(diagramJson: { cells?: unknown } | null | undefined): void {
    const graph = parseDiagram(diagramJson);
    if (!graph.nodes.length && !graph.zones.length) {
      this.loading.set(false);
      this.empty.set(true);
      return;
    }
    this.zone.runOutsideAngular(() => {
      TopologyScene.create(this.canvasRef.nativeElement, graph, this.motion.reducedMotion())
        .then((scene) => {
          if (this.destroyed) {
            scene.destroy();
            return;
          }
          this.scene = scene;
          scene.onHover = (info) => this.zone.run(() => this.hover.set(info));
          this.zone.run(() => this.loading.set(false));
        })
        .catch(() => this.zone.run(() => {
          this.loading.set(false);
          this.failed.set(true);
        }));
    });
  }

  private teardownScene(): void {
    this.scene?.destroy();
    this.scene = undefined;
    this.hover.set(null);
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.teardownScene();
  }
}
