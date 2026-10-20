import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import type { RangeDiagram } from '@core/models';
import { ApiService } from '@core/services/api.service';
import { TOPOLOGY_TYPE_NAMES, Topo2d, Topo2dNode, layoutTopology2d } from './topology-2d';

/** Status words -> legend entry. Anything else (provisioning, failed, ...) is shown as written. */
const STATUS_CLASS: Record<string, string> = {
  running: 'st-running', stopped: 'st-stopped', planned: 'st-planned', 'not built': 'st-missing', failed: 'st-failed',
};

/**
 * A range's topology as a read-only 2D picture, for the range detail and exercise pages.
 *
 * Reads GET /ranges/{id}/diagram: the saved Range Designer diagram, or (no saved one)
 * the topology the API draws from the range's template and its last build, labelled as
 * such. Networks are the dashed zones, hosts sit on the network they are on, and lines
 * are a firewall's or sensor's NICs on other networks. The same cells feed the 3D view.
 */
@Component({
  selector: 'tn-range-topology',
  imports: [MatButtonModule, MatIconModule, MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (loading()) {
      <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading topology"></div>
    } @else if (error()) {
      <p class="muted" role="alert">
        Could not load the topology: {{ error() }}
        <button mat-button (click)="load()">Retry</button>
      </p>
    } @else if (diagram(); as d) {
      @if (!picture().nodes.length && !picture().zones.length) {
        <p class="muted" role="status">
          {{ d.notes?.[0] || 'Nothing to draw: this range has no saved diagram and its template declares no hosts.' }}
        </p>
      } @else {
        <div class="bar">
          <span class="source" [class.generated]="d.generated">
            <mat-icon inline>{{ d.generated ? 'auto_awesome' : 'architecture' }}</mat-icon>
            {{ d.generated ? 'Drawn from the template' : 'Saved designer diagram' }}
          </span>
          <span class="muted counts">{{ picture().zones.length }} networks · {{ picture().nodes.length }} hosts</span>
          <span class="spacer"></span>
          <button mat-button (click)="actualSize.set(!actualSize())" [attr.aria-pressed]="actualSize()"
                  [matTooltip]="actualSize() ? 'Fit the picture to the panel' : 'Show at full size (scroll to pan)'">
            <mat-icon>{{ actualSize() ? 'fit_screen' : 'zoom_in' }}</mat-icon> {{ actualSize() ? 'Fit' : 'Full size' }}
          </button>
        </div>
        @if (d.generated) {
          <ul class="notes muted">
            @for (n of d.notes ?? []; track $index) { <li>{{ n }}</li> }
          </ul>
        }
        @if (!actualSize() && picture().box.w > 1400) {
          <p class="muted wide-hint">Wide topology: scroll the picture sideways to see every network.</p>
        }
        <div class="topo" tabindex="0" role="region" [attr.aria-label]="'Topology of ' + picture().nodes.length + ' hosts; scroll to pan'">
          @let p = picture();
          <svg [attr.viewBox]="p.box.x + ' ' + p.box.y + ' ' + p.box.w + ' ' + p.box.h"
               preserveAspectRatio="xMidYMin meet" class="topo-svg" aria-hidden="true"
               [style.width]="actualSize() ? p.box.w + 'px' : '100%'"
               [style.min-width.px]="actualSize() ? p.box.w : p.box.w * 0.62">
            @for (z of p.zones; track z.id) {
              <rect [attr.x]="z.x" [attr.y]="z.y" [attr.width]="z.w" [attr.height]="z.h" rx="12"
                    [attr.fill]="z.color + '14'" [attr.stroke]="z.color" stroke-width="1.5" stroke-dasharray="9 6" />
              <text [attr.x]="z.x + 14" [attr.y]="z.y + 22" [attr.fill]="z.color" font-size="15" font-weight="700">{{ z.label }}</text>
              <text [attr.x]="z.x + 14" [attr.y]="z.y + 40" [attr.fill]="z.color" font-size="12" opacity="0.85">{{ z.cidr }}</text>
            }
            @for (l of p.links; track l.id) {
              <line [attr.x1]="l.x1" [attr.y1]="l.y1" [attr.x2]="l.x2" [attr.y2]="l.y2" class="link"
                    [attr.stroke-dasharray]="l.monitor ? '6 4' : null" />
              @if (l.ip) {
                <text class="link-ip" [attr.x]="(l.x1 + l.x2) / 2 + 4" [attr.y]="(l.y1 + l.y2) / 2" font-size="11">{{ l.ip }}</text>
              }
            }
            @for (n of p.nodes; track n.id) {
              <g>
                <title>{{ n.title }}</title>
                <rect class="node-box" [attr.x]="n.x" [attr.y]="n.y" [attr.width]="n.w" [attr.height]="n.h" rx="9"
                      [attr.stroke]="n.color" stroke-width="1.5" />
                <rect [attr.x]="n.x" [attr.y]="n.y" width="6" [attr.height]="n.h" rx="3" [attr.fill]="n.color" />
                <text class="node-title" [attr.x]="n.x + 16" [attr.y]="n.y + lineY(n, 0)" font-size="13" font-weight="600">{{ n.shortLabel }}</text>
                <text class="node-sub" [attr.x]="n.x + 16" [attr.y]="n.y + lineY(n, 1)" font-size="11">{{ typeLine(n) }}</text>
                @if (n.ip) {
                  <text class="node-ip" [attr.x]="n.x + 16" [attr.y]="n.y + lineY(n, 2)" font-size="11">{{ n.ip }}</text>
                }
                @if (n.status) {
                  <circle [attr.cx]="n.x + n.w - 12" [attr.cy]="n.y + 12" r="5" class="dot" [class]="statusClass(n.status)" />
                }
              </g>
            }
          </svg>
        </div>
        @if (statuses().length) {
          <p class="legend muted">
            @for (s of statuses(); track s) {
              <span><svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="5" class="dot" [class]="statusClass(s)" /></svg> {{ s }}</span>
            }
            @if (d.generated) { <span>· status is the range's state, not read per VM</span> }
          </p>
        }
        <!-- The same picture as text, by network, for screen readers -->
        <ul class="sr-only" aria-label="Hosts by network">
          @for (z of picture().zones; track z.id) {
            <li>{{ z.label }} {{ z.cidr }}:
              @for (n of hostsIn(z.id); track n.id) { {{ n.title }}; }
            </li>
          }
          @if (between().length) {
            <li>Between networks: @for (n of between(); track n.id) { {{ n.title }}; }</li>
          }
        </ul>
      }
    }
  `,
  styles: [`
    :host { display: block; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .spacer { flex: 1; }
    .source { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 600;
              padding: 2px 10px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-secondary); }
    .source.generated { border-color: var(--accent); color: var(--accent); }
    .notes { margin: 6px 0 0; padding-left: 18px; font-size: 12px; }
    .wide-hint { margin: 6px 0 0; font-size: 12px; }
    .topo { width: 100%; max-height: 70vh; overflow: auto; border-radius: 10px; background: var(--bg-secondary);
            border: 1px solid var(--border); margin: 10px 0 6px; }
    .topo:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .topo-svg { height: auto; display: block; }
    .topo-svg .node-box { fill: var(--bg-surface); }
    .topo-svg .node-title { fill: var(--text-primary); font-family: var(--font-body, Inter, sans-serif); }
    .topo-svg .node-sub { fill: var(--text-secondary); font-family: var(--font-body, Inter, sans-serif); }
    .topo-svg .node-ip { fill: var(--text-primary); font-family: var(--font-mono, monospace); }
    .topo-svg .link { stroke: var(--text-muted); stroke-width: 2; }
    .topo-svg .link-ip { fill: var(--text-secondary); font-family: var(--font-mono, monospace); }
    .dot { stroke-width: 1.5; }
    .st-running { fill: var(--success, #22c55e); stroke: var(--success, #22c55e); }
    .st-stopped { fill: var(--text-muted); stroke: var(--text-muted); }
    .st-planned { fill: none; stroke: var(--text-secondary); }
    .st-missing { fill: var(--warning, #f59e0b); stroke: var(--warning, #f59e0b); }
    .st-failed { fill: var(--alert, #ef4444); stroke: var(--alert, #ef4444); }
    .st-other { fill: var(--accent); stroke: var(--accent); }
    .legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 0; font-size: 12px; }
    .legend span { display: inline-flex; align-items: center; gap: 4px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class RangeTopologyComponent {
  private api = inject(ApiService);

  readonly rangeId = input.required<string>();

  readonly diagram = signal<RangeDiagram | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly actualSize = signal(false);

  readonly picture = computed<Topo2d>(() => layoutTopology2d(this.diagram()?.diagram_json as { cells?: unknown }));
  readonly statuses = computed(() => [...new Set(this.picture().nodes.map(n => n.status).filter(Boolean))]);

  constructor() {
    effect(() => {
      this.rangeId();
      untracked(() => this.load());
    });
  }

  load(): void {
    const id = this.rangeId();
    if (!id) return;
    this.loading.set(true);
    this.error.set('');
    this.api.getRangeDiagram(id).subscribe({
      next: d => {
        this.diagram.set(d);
        this.loading.set(false);
      },
      error: err => {
        // A failed read is an error, never an empty picture.
        this.diagram.set(null);
        this.error.set(err?.error?.detail || err?.message || 'the request failed');
        this.loading.set(false);
      },
    });
  }

  hostsIn(zoneId: string): Topo2dNode[] {
    const z = this.picture().zones.find(zone => zone.id === zoneId);
    return z ? this.picture().nodes.filter(n => this.inZone(n, z)) : [];
  }

  /** The OS when known (the type is in the colour, tooltip and text list), else the type. */
  typeLine(n: Topo2dNode): string {
    const text = n.os || TOPOLOGY_TYPE_NAMES[n.type] || n.type;
    const max = Math.max(4, Math.floor((n.w - 26) / 6));
    return text.length <= max ? text : text.slice(0, max - 1) + '…';
  }

  /** Hosts in no zone: the firewalls and routers between networks, and the internet. */
  readonly between = computed(() => {
    const zones = this.picture().zones;
    return this.picture().nodes.filter(n => !zones.some(z => this.inZone(n, z)));
  });

  private inZone(n: Topo2dNode, z: { x: number; y: number; w: number; h: number }): boolean {
    const cx = n.x + n.w / 2;
    const cy = n.y + n.h / 2;
    return cx >= z.x && cx <= z.x + z.w && cy >= z.y && cy <= z.y + z.h;
  }

  /** Baselines for the three text lines, spread over the box height. */
  lineY(n: Topo2dNode, line: number): number {
    const lines = n.ip ? 3 : 2;
    const top = (n.h - lines * 16) / 2 + 12;
    return top + line * 17;
  }

  statusClass(status: string): string {
    return 'dot ' + (STATUS_CLASS[status] ?? 'st-other');
  }
}
