import { AfterViewInit, Component, ElementRef, NgZone, OnDestroy, OnInit, ViewChild, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterModule } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';

import { ApiService } from '@core/services/api.service';
import { Range } from '@core/models';
import { MotionService } from '../../shared/motion';
import { HoverInfo, TopologyScene, parseDiagram } from './topology-scene';

@Component({
  selector: 'tn-topology-3d',
  standalone: true,
  imports: [
    CommonModule, RouterModule, FormsModule, MatButtonModule, MatCardModule,
    MatFormFieldModule, MatIconModule, MatSelectModule, MatTooltipModule,
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
          <button mat-stroked-button [routerLink]="['/authoring/ranges']" [queryParams]="{ range: selectedRangeId }"
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
        @if (!loading() && empty()) {
          <div class="overlay-message">
            <mat-icon>category</mat-icon>
            <p>No saved diagram for this range.</p>
            <p class="hint">Draw one in the Range Designer and hit Save — it will appear here in 3D.</p>
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
  @ViewChild('canvas') canvasRef!: ElementRef<HTMLCanvasElement>;

  ranges = signal<Range[]>([]);
  loading = signal(false);
  empty = signal(false);
  hover = signal<HoverInfo | null>(null);

  selectedRangeId = '';

  private scene?: TopologyScene;
  private viewReady = false;
  private destroyed = false;

  constructor(
    private api: ApiService,
    private route: ActivatedRoute,
    private motion: MotionService,
    private zone: NgZone,
  ) {}

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
      error: () => this.empty.set(true),
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
    this.api.getRangeDiagram(this.selectedRangeId).subscribe({
      next: ({ diagram_json }) => this.buildScene(diagram_json),
      error: () => {
        this.loading.set(false);
        this.empty.set(true);
      },
    });
  }

  private buildScene(diagramJson: any): void {
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
          this.empty.set(true);
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
