import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { HypervisorNode } from '@core/models';
import { LoadGaugeComponent, loadPct } from '../../shared/components/load-gauge/load-gauge.component';

/** One host's load at a glance: name, status and CPU / Memory / Storage gauges.
 *  Shared by the Overview and Compute tabs. Unreported figures render "not reported". */
@Component({
  selector: 'tn-host-load-tile',
  imports: [MatCardModule, MatIconModule, LoadGaugeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <mat-card class="host-tile">
      <div class="host-tile-head">
        <mat-icon [class]="online() ? 'status-online' : 'status-offline'" aria-hidden="true">
          {{ online() ? 'check_circle' : 'cancel' }}
        </mat-icon>
        <span class="host-name">{{ node().node_name }}</span>
        <span class="sr-only">{{ online() ? 'online' : 'offline' }}</span>
      </div>
      <div class="gauge-row">
        <tn-load-gauge label="CPU" [value]="node().cpu_used" />
        <tn-load-gauge label="Memory" [value]="memory()" />
        <tn-load-gauge label="Storage" [value]="storage()" />
      </div>
    </mat-card>
  `,
  styles: [`
    :host { display: block; }
    .host-tile { padding: 12px; background: var(--bg-card); border: 1px solid var(--border); height: 100%; box-sizing: border-box; }
    .host-tile-head { display: flex; align-items: center; gap: 8px; }
    .host-name { font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .status-online { color: var(--success); }
    .status-offline { color: var(--alert); }
    .gauge-row { display: flex; flex-wrap: wrap; justify-content: space-around; gap: 8px; padding-top: 8px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class HostLoadTileComponent {
  readonly node = input.required<HypervisorNode>();
  readonly online = computed(() => this.node().status === 'online');
  readonly memory = computed(() => loadPct(this.node().memory_used_gb, this.node().memory_total_gb));
  readonly storage = computed(() => loadPct(this.node().storage_used_gb, this.node().storage_total_gb));
}
