import { ChangeDetectionStrategy, ChangeDetectorRef, Component, Input, OnChanges, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatTableModule } from '@angular/material/table';
import { HypervisorConnection, InventoryVm, VmGroup, VmInventory } from '@core/models';
import { InfrastructureApiService } from '@core/services/infrastructure-api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

/** The four groups, in the order the page shows them. Tags decide the group (the API's
 *  `group`, from tn-type), never names or folders. */
export const VM_GROUPS: { id: VmGroup; label: string }[] = [
  { id: 'golden', label: 'Golden templates' },
  { id: 'copies', label: 'Host copies' },
  { id: 'ranges', label: 'Range VMs' },
  { id: 'other', label: 'Other' },
];

/**
 * A connection's VMs, read live from vCenter and grouped by TrueNorth's tn-type tag.
 * Loaded on request (a large vCenter takes seconds), then filtered here by group.
 */
@Component({
  selector: 'tn-vm-inventory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule, MatButtonModule, MatChipsModule, MatFormFieldModule, MatIconModule, MatProgressBarModule,
    MatSelectModule, MatTableModule, EmptyStateComponent,
  ],
  template: `
    @if (connections.length) {
    <div class="bar">
      @if (connections.length > 1) {
        <mat-form-field appearance="outline" subscriptSizing="dynamic">
          <mat-label>Connection</mat-label>
          <mat-select data-testid="vm-conn" [(ngModel)]="connectionId" (selectionChange)="inventory = null"
            panelClass="tn-select-panel">
            @for (c of connections; track c.id) {
              <mat-option [value]="c.id">{{ c.name }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
      }
      <button mat-stroked-button data-testid="vm-load" (click)="load()" [disabled]="loading || !connectionId">
        <mat-icon>{{ inventory ? 'refresh' : 'download' }}</mat-icon>
        {{ inventory ? 'Reload' : 'Load VMs' }}
      </button>
    </div>
    }
    @if (loading) {
      <mat-progress-bar mode="indeterminate" />
    }

    @if (!connections.length) {
      <tn-empty-state icon="dns" title="No hypervisor connection"
        message="Add a connection on the Compute tab to list its virtual machines." />
    } @else if (inventory && !inventory.supported) {
      <tn-empty-state icon="block" title="Not available" [message]="inventory.message" />
    } @else if (inventory) {
      <mat-chip-listbox data-testid="vm-groups" aria-label="Filter by type" [(ngModel)]="group"
        [hideSingleSelectionIndicator]="true">
        <mat-chip-option value="all">All ({{ inventory.vms?.length ?? 0 }})</mat-chip-option>
        @for (g of groups; track g.id) {
          <mat-chip-option [value]="g.id">{{ g.label }} ({{ count(g.id) }})</mat-chip-option>
        }
      </mat-chip-listbox>
      @if (inventory.message) {
        <p class="note" data-testid="vm-note">{{ inventory.message }}</p>
      }
      @if (shown.length) {
        <div class="table-container">
          <table mat-table [dataSource]="shown" class="full-width">
            <ng-container matColumnDef="name">
              <th mat-header-cell *matHeaderCellDef>Name</th>
              <td mat-cell *matCellDef="let v">{{ v.name }}</td>
            </ng-container>
            <ng-container matColumnDef="type">
              <th mat-header-cell *matHeaderCellDef>Type</th>
              <td mat-cell *matCellDef="let v">{{ tag(v, 'tn-type') }}</td>
            </ng-container>
            <ng-container matColumnDef="os">
              <th mat-header-cell *matHeaderCellDef>OS</th>
              <td mat-cell *matCellDef="let v">{{ tag(v, 'tn-os') }}</td>
            </ng-container>
            <ng-container matColumnDef="template">
              <th mat-header-cell *matHeaderCellDef>Template</th>
              <td mat-cell *matCellDef="let v">{{ tag(v, 'tn-template') }}</td>
            </ng-container>
            <ng-container matColumnDef="range">
              <th mat-header-cell *matHeaderCellDef>Range</th>
              <td mat-cell *matCellDef="let v">{{ tag(v, 'tn-range') }}</td>
            </ng-container>
            <ng-container matColumnDef="host">
              <th mat-header-cell *matHeaderCellDef>Host</th>
              <td mat-cell *matCellDef="let v">{{ v.host || '—' }}</td>
            </ng-container>
            <ng-container matColumnDef="power">
              <th mat-header-cell *matHeaderCellDef>Power</th>
              <td mat-cell *matCellDef="let v">{{ power(v) }}</td>
            </ng-container>
            <tr mat-header-row *matHeaderRowDef="cols"></tr>
            <tr mat-row *matRowDef="let row; columns: cols;"></tr>
          </table>
        </div>
      } @else {
        <tn-empty-state icon="inventory_2" title="No VMs in this group" />
      }
    } @else if (!loading) {
      <tn-empty-state icon="computer" title="Virtual machines"
        message="Load the connection's VMs, grouped by their TrueNorth tags: golden templates, host copies, range VMs and the rest." />
    }
  `,
  styles: [`
    :host { display: block; margin-top: 16px; }
    .bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
    mat-chip-listbox { display: block; margin: 8px 0 12px; }
    .note { color: var(--text-secondary); font-size: 13px; margin: 0 0 8px; }
    .full-width { width: 100%; }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
  `],
})
export class VmInventoryComponent implements OnChanges {
  private infra = inject(InfrastructureApiService);
  private cdr = inject(ChangeDetectorRef);

  @Input() connections: HypervisorConnection[] = [];

  readonly groups = VM_GROUPS;
  readonly cols = ['name', 'type', 'os', 'template', 'range', 'host', 'power'];
  connectionId: string | null = null;
  inventory: VmInventory | null = null;
  group: VmGroup | 'all' = 'all';
  loading = false;

  ngOnChanges(): void {
    if (!this.connections.some(c => c.id === this.connectionId)) {
      this.connectionId = (this.connections.find(c => c.is_primary) ?? this.connections[0])?.id ?? null;
      this.inventory = null;
    }
  }

  get shown(): InventoryVm[] {
    const vms = this.inventory?.vms ?? [];
    return this.group === 'all' ? vms : vms.filter(v => v.group === this.group);
  }

  count(g: VmGroup): number {
    return this.inventory?.groups?.[g] ?? 0;
  }

  tag(v: InventoryVm, category: string): string {
    return v.tags?.[category] || '—';
  }

  power(v: InventoryVm): string {
    switch (v.power_state) {
      case 'POWERED_ON': return 'On';
      case 'POWERED_OFF': return 'Off';
      case 'SUSPENDED': return 'Suspended';
      default: return '—';
    }
  }

  load(): void {
    if (!this.connectionId || this.loading) return;
    this.loading = true;
    this.infra.connectionVms(this.connectionId).subscribe({
      next: inv => { this.inventory = inv; this.loading = false; this.cdr.markForCheck(); },
      error: e => {
        this.inventory = { supported: true, message: 'Could not load VMs: ' + (e.error?.detail || e.message), vms: [] };
        this.loading = false;
        this.cdr.markForCheck();
      },
    });
  }
}
