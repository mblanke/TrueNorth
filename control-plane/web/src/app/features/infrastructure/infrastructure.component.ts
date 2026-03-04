import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatBadgeModule } from '@angular/material/badge';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

/* ── Local interfaces (match backend schemas) ───────────────── */
interface HypervisorConnection {
  id: string; name: string; hypervisor_type: 'proxmox' | 'vsphere' | 'hyperv';
  host: string; port: number; username: string; verify_ssl: boolean;
  is_primary: boolean; is_active: boolean; datacenter: string | null;
  notes: string | null; created_at: string;
}
interface HypervisorNode {
  id: string; node_name: string; ip_address: string | null; status: string;
  cpu_total: number | null; cpu_used: number | null;
  memory_total_gb: number | null; memory_used_gb: number | null;
  storage_total_gb: number | null; storage_used_gb: number | null;
  vm_count: number;
}
interface HypervisorSummary {
  total_connections: number; active_connections: number;
  total_nodes: number; online_nodes: number; total_vms: number;
  total_cpu: number; total_memory_gb: number; total_storage_gb: number;
  by_type: Record<string, number>;
}
type StorageProtocol = 'nfs' | 'iscsi' | 'fc' | 'nvme_of' | 'smb';
interface StorageAppliance {
  id: string; name: string; vendor: string; model: string;
  management_ip: string; protocol: StorageProtocol;
  raw_capacity_tb: number; usable_capacity_tb: number;
  is_active: boolean; notes: string | null; created_at: string;
}
interface StorageVolume {
  id: string; appliance_id: string; volume_name: string;
  size_gb: number; used_gb: number; protocol: StorageProtocol;
  mount_path: string | null; created_at: string;
}
interface StorageSummary {
  total_appliances: number; active_appliances: number;
  total_raw_tb: number; total_usable_tb: number; total_volumes: number;
}
type NetworkDeviceRole = 'tor' | 'spine' | 'leaf' | 'firewall' | 'router' | 'oob';
interface NetworkDevice {
  id: string; name: string; vendor: string; model: string;
  role: NetworkDeviceRole; management_ip: string;
  firmware_version: string | null; port_count: number;
  is_active: boolean; notes: string | null; created_at: string;
}
interface NetworkSummary {
  total_devices: number; active_devices: number;
  by_role: Record<string, number>;
}

@Component({
  selector: 'app-infrastructure',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatTabsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatTableModule, MatChipsModule, MatDialogModule,
    MatFormFieldModule, MatInputModule, MatSelectModule,
    MatSlideToggleModule, MatProgressBarModule, MatTooltipModule,
    MatBadgeModule, MatSnackBarModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">dns</mat-icon>
          <div>
            <h1>Infrastructure</h1>
            <p class="subtitle">Hypervisors, compute, storage &amp; network management</p>
          </div>
        </div>
      </div>

      <mat-tab-group animationDuration="200ms" color="primary">
        <!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
        <mat-tab>
          <ng-template mat-tab-label><mat-icon class="tab-icon">dashboard</mat-icon> Overview</ng-template>

          <!-- Summary cards -->
          <div class="summary-row" *ngIf="hvSummary">
            <mat-card class="stat-card">
              <mat-icon>link</mat-icon>
              <div class="stat-value">{{ hvSummary.total_connections }}</div>
              <div class="stat-label">Connections</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>computer</mat-icon>
              <div class="stat-value">{{ hvSummary.total_nodes }}</div>
              <div class="stat-label">Nodes</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>memory</mat-icon>
              <div class="stat-value">{{ hvSummary.total_cpu }}</div>
              <div class="stat-label">vCPUs</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>dynamic_form</mat-icon>
              <div class="stat-value">{{ hvSummary.total_memory_gb | number:'1.0-0' }} GB</div>
              <div class="stat-label">RAM</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>storage</mat-icon>
              <div class="stat-value">{{ hvSummary.total_storage_gb | number:'1.0-0' }} GB</div>
              <div class="stat-label">Disk</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>cloud</mat-icon>
              <div class="stat-value">{{ hvSummary.total_vms }}</div>
              <div class="stat-label">VMs</div>
            </mat-card>
          </div>

          <mat-card class="notice-card" *ngIf="hvSummary && connections.length > 0 && hvSummary.total_nodes === 0">
            <mat-card-content>
              <div class="notice-content">
                <mat-icon>info</mat-icon>
                <div>
                  <strong>Connections are configured, but no compute nodes are discovered yet.</strong>
                  <p>Click <em>Discover Nodes</em> on a connection, or use <em>Discover All Nodes</em> in the Compute tab to populate node, CPU, memory, storage, and VM metrics.</p>
                </div>
              </div>
            </mat-card-content>
          </mat-card>

          <!-- Quick counts for storage & network -->
          <div class="summary-row" *ngIf="storageSummary || networkSummary">
            <mat-card class="stat-card" *ngIf="storageSummary">
              <mat-icon>inventory_2</mat-icon>
              <div class="stat-value">{{ storageSummary.total_appliances }}</div>
              <div class="stat-label">Storage Appliances</div>
            </mat-card>
            <mat-card class="stat-card" *ngIf="storageSummary">
              <mat-icon>disc_full</mat-icon>
              <div class="stat-value">{{ storageSummary.total_usable_tb | number:'1.1-1' }} TB</div>
              <div class="stat-label">Usable Storage</div>
            </mat-card>
            <mat-card class="stat-card" *ngIf="networkSummary">
              <mat-icon>router</mat-icon>
              <div class="stat-value">{{ networkSummary.total_devices }}</div>
              <div class="stat-label">Network Devices</div>
            </mat-card>
          </div>
        </mat-tab>

        <!-- ═══════════════ COMPUTE TAB ═══════════════ -->
        <mat-tab>
          <ng-template mat-tab-label><mat-icon class="tab-icon">computer</mat-icon> Compute</ng-template>

          <div class="tab-actions">
            <button mat-raised-button color="primary" (click)="showAddConn = !showAddConn">
              <mat-icon>add</mat-icon> Add Connection
            </button>
            <button mat-stroked-button (click)="discoverAllNodes()" [disabled]="discoveringAll || connections.length === 0">
              <mat-icon>travel_explore</mat-icon>
              {{ discoveringAll ? 'Discovering...' : 'Discover All Nodes' }}
            </button>
          </div>

          <!-- Add Connection Form -->
          <mat-card *ngIf="showAddConn" class="add-form-card">
            <mat-card-header><mat-card-title>New Hypervisor Connection</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Name</mat-label>
                  <input matInput [(ngModel)]="newConn.name" placeholder="Production Proxmox">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Type</mat-label>
                  <mat-select [(ngModel)]="newConn.hypervisor_type" panelClass="tn-select-panel">
                    <mat-option value="proxmox">Proxmox VE</mat-option>
                    <mat-option value="vsphere">VMware vSphere</mat-option>
                    <mat-option value="hyperv">Microsoft Hyper-V</mat-option>
                  </mat-select>
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Host</mat-label>
                  <input matInput [(ngModel)]="newConn.host" placeholder="192.168.1.85">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Port</mat-label>
                  <input matInput type="number" [(ngModel)]="newConn.port">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Username</mat-label>
                  <input matInput [(ngModel)]="newConn.username" placeholder="root@pam">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Password / Token</mat-label>
                  <input matInput type="password" [(ngModel)]="newConn.password">
                </mat-form-field>
              </div>
            </mat-card-content>
            <mat-card-actions>
              <button mat-raised-button color="primary" (click)="createConnection()">Save</button>
              <button mat-button (click)="showAddConn = false">Cancel</button>
            </mat-card-actions>
          </mat-card>

          <!-- Connections table -->
          <mat-card>
            <mat-card-header><mat-card-title>Hypervisor Connections</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="table-wrap">
                <table mat-table [dataSource]="connections" class="full-width">
                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let c">
                      <mat-icon [class]="c.is_active ? 'status-online' : 'status-offline'">
                        {{ c.is_active ? 'check_circle' : 'cancel' }}
                      </mat-icon>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="name">
                    <th mat-header-cell *matHeaderCellDef>Name</th>
                    <td mat-cell *matCellDef="let c">
                      {{ c.name }}
                      <mat-icon *ngIf="c.is_primary" class="primary-badge" matTooltip="Primary">star</mat-icon>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="type">
                    <th mat-header-cell *matHeaderCellDef>Type</th>
                    <td mat-cell *matCellDef="let c">
                      <mat-chip-set><mat-chip [class]="'type-' + c.hypervisor_type">{{ c.hypervisor_type | uppercase }}</mat-chip></mat-chip-set>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="host">
                    <th mat-header-cell *matHeaderCellDef>Host</th>
                    <td mat-cell *matCellDef="let c">{{ c.host }}:{{ c.port }}</td>
                  </ng-container>
                  <ng-container matColumnDef="actions">
                    <th mat-header-cell *matHeaderCellDef>Actions</th>
                    <td mat-cell *matCellDef="let c">
                      <button mat-icon-button matTooltip="Test" (click)="testConnection(c)"><mat-icon>network_check</mat-icon></button>
                      <button mat-icon-button matTooltip="Discover Nodes" (click)="discoverNodes(c)"><mat-icon>search</mat-icon></button>
                      <button mat-icon-button matTooltip="Set Primary" (click)="setPrimary(c)" [disabled]="c.is_primary"><mat-icon>star_border</mat-icon></button>
                      <button mat-icon-button matTooltip="Delete" color="warn" (click)="deleteConnection(c)"><mat-icon>delete</mat-icon></button>
                    </td>
                  </ng-container>
                  <tr mat-header-row *matHeaderRowDef="connCols"></tr>
                  <tr mat-row *matRowDef="let row; columns: connCols;"></tr>
                </table>
              </div>
              <p *ngIf="connections.length === 0" class="empty-state">No hypervisor connections configured.</p>
            </mat-card-content>
          </mat-card>

          <!-- Compute Nodes -->
          <mat-card *ngIf="allNodes.length" style="margin-top:16px">
            <mat-card-header><mat-card-title>Compute Nodes</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="table-wrap">
                <table mat-table [dataSource]="allNodes" class="full-width">
                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let n">
                      <mat-icon [class]="n.status === 'online' ? 'status-online' : 'status-offline'">
                        {{ n.status === 'online' ? 'check_circle' : 'cancel' }}
                      </mat-icon>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="node_name">
                    <th mat-header-cell *matHeaderCellDef>Node</th>
                    <td mat-cell *matCellDef="let n">{{ n.node_name }}</td>
                  </ng-container>
                  <ng-container matColumnDef="ip">
                    <th mat-header-cell *matHeaderCellDef>IP</th>
                    <td mat-cell *matCellDef="let n">{{ n.ip_address || '—' }}</td>
                  </ng-container>
                  <ng-container matColumnDef="cpu">
                    <th mat-header-cell *matHeaderCellDef>CPU</th>
                    <td mat-cell *matCellDef="let n">{{ n.cpu_total }} cores {{ n.cpu_used != null ? '(' + (n.cpu_used | number:'1.0-0') + '%)' : '' }}</td>
                  </ng-container>
                  <ng-container matColumnDef="memory">
                    <th mat-header-cell *matHeaderCellDef>Memory</th>
                    <td mat-cell *matCellDef="let n">{{ n.memory_used_gb | number:'1.1-1' }} / {{ n.memory_total_gb | number:'1.1-1' }} GB</td>
                  </ng-container>
                  <ng-container matColumnDef="storage">
                    <th mat-header-cell *matHeaderCellDef>Storage</th>
                    <td mat-cell *matCellDef="let n">{{ n.storage_used_gb | number:'1.0-0' }} / {{ n.storage_total_gb | number:'1.0-0' }} GB</td>
                  </ng-container>
                  <ng-container matColumnDef="vms">
                    <th mat-header-cell *matHeaderCellDef>VMs</th>
                    <td mat-cell *matCellDef="let n">{{ n.vm_count }}</td>
                  </ng-container>
                  <tr mat-header-row *matHeaderRowDef="nodeCols"></tr>
                  <tr mat-row *matRowDef="let row; columns: nodeCols;"></tr>
                </table>
              </div>
            </mat-card-content>
          </mat-card>
        </mat-tab>

        <!-- ═══════════════ STORAGE TAB ═══════════════ -->
        <mat-tab>
          <ng-template mat-tab-label><mat-icon class="tab-icon">inventory_2</mat-icon> Storage</ng-template>

          <div class="tab-actions">
            <button mat-raised-button color="primary" (click)="showAddStorage = !showAddStorage">
              <mat-icon>add</mat-icon> Add Appliance
            </button>
          </div>

          <!-- Add Appliance Form -->
          <mat-card *ngIf="showAddStorage" class="add-form-card">
            <mat-card-header><mat-card-title>New Storage Appliance</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Name</mat-label>
                  <input matInput [(ngModel)]="newAppliance.name" placeholder="NetApp AFF A250">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Vendor</mat-label>
                  <input matInput [(ngModel)]="newAppliance.vendor" placeholder="NetApp">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Model</mat-label>
                  <input matInput [(ngModel)]="newAppliance.model" placeholder="AFF A250">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Management IP</mat-label>
                  <input matInput [(ngModel)]="newAppliance.management_ip" placeholder="10.0.60.10">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Protocol</mat-label>
                  <mat-select [(ngModel)]="newAppliance.protocol" panelClass="tn-select-panel">
                    <mat-option value="nfs">NFS</mat-option>
                    <mat-option value="iscsi">iSCSI</mat-option>
                    <mat-option value="fc">Fibre Channel</mat-option>
                    <mat-option value="nvme_of">NVMe-oF</mat-option>
                    <mat-option value="smb">SMB</mat-option>
                  </mat-select>
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Raw Capacity (TB)</mat-label>
                  <input matInput type="number" [(ngModel)]="newAppliance.raw_capacity_tb">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Usable Capacity (TB)</mat-label>
                  <input matInput type="number" [(ngModel)]="newAppliance.usable_capacity_tb">
                </mat-form-field>
              </div>
            </mat-card-content>
            <mat-card-actions>
              <button mat-raised-button color="primary" (click)="createAppliance()">Save</button>
              <button mat-button (click)="showAddStorage = false">Cancel</button>
            </mat-card-actions>
          </mat-card>

          <!-- Summary -->
          <div class="summary-row" *ngIf="storageSummary">
            <mat-card class="stat-card">
              <mat-icon>inventory_2</mat-icon>
              <div class="stat-value">{{ storageSummary.total_appliances }}</div>
              <div class="stat-label">Appliances</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>check_circle</mat-icon>
              <div class="stat-value">{{ storageSummary.active_appliances }}</div>
              <div class="stat-label">Active</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>disc_full</mat-icon>
              <div class="stat-value">{{ storageSummary.total_raw_tb | number:'1.1-1' }} TB</div>
              <div class="stat-label">Raw</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>storage</mat-icon>
              <div class="stat-value">{{ storageSummary.total_usable_tb | number:'1.1-1' }} TB</div>
              <div class="stat-label">Usable</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>topic</mat-icon>
              <div class="stat-value">{{ storageSummary.total_volumes }}</div>
              <div class="stat-label">Volumes</div>
            </mat-card>
          </div>

          <!-- Appliances Table -->
          <mat-card>
            <mat-card-header><mat-card-title>Storage Appliances</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="table-wrap">
                <table mat-table [dataSource]="appliances" class="full-width">
                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let a">
                      <mat-icon [class]="a.is_active ? 'status-online' : 'status-offline'">
                        {{ a.is_active ? 'check_circle' : 'cancel' }}
                      </mat-icon>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="name">
                    <th mat-header-cell *matHeaderCellDef>Name</th>
                    <td mat-cell *matCellDef="let a">{{ a.name }}</td>
                  </ng-container>
                  <ng-container matColumnDef="vendor">
                    <th mat-header-cell *matHeaderCellDef>Vendor</th>
                    <td mat-cell *matCellDef="let a">{{ a.vendor }}</td>
                  </ng-container>
                  <ng-container matColumnDef="model">
                    <th mat-header-cell *matHeaderCellDef>Model</th>
                    <td mat-cell *matCellDef="let a">{{ a.model }}</td>
                  </ng-container>
                  <ng-container matColumnDef="ip">
                    <th mat-header-cell *matHeaderCellDef>Mgmt IP</th>
                    <td mat-cell *matCellDef="let a">{{ a.management_ip }}</td>
                  </ng-container>
                  <ng-container matColumnDef="protocol">
                    <th mat-header-cell *matHeaderCellDef>Protocol</th>
                    <td mat-cell *matCellDef="let a">
                      <mat-chip-set><mat-chip>{{ a.protocol | uppercase }}</mat-chip></mat-chip-set>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="capacity">
                    <th mat-header-cell *matHeaderCellDef>Capacity</th>
                    <td mat-cell *matCellDef="let a">{{ a.usable_capacity_tb | number:'1.1-1' }} / {{ a.raw_capacity_tb | number:'1.1-1' }} TB</td>
                  </ng-container>
                  <ng-container matColumnDef="actions">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let a">
                      <button mat-icon-button matTooltip="Delete" color="warn" (click)="deleteAppliance(a)"><mat-icon>delete</mat-icon></button>
                    </td>
                  </ng-container>
                  <tr mat-header-row *matHeaderRowDef="applianceCols"></tr>
                  <tr mat-row *matRowDef="let row; columns: applianceCols;"></tr>
                </table>
              </div>
              <p *ngIf="appliances.length === 0" class="empty-state">No storage appliances registered.</p>
            </mat-card-content>
          </mat-card>
        </mat-tab>

        <!-- ═══════════════ NETWORK TAB ═══════════════ -->
        <mat-tab>
          <ng-template mat-tab-label><mat-icon class="tab-icon">router</mat-icon> Network</ng-template>

          <div class="tab-actions">
            <button mat-raised-button color="primary" (click)="showAddNetwork = !showAddNetwork">
              <mat-icon>add</mat-icon> Add Device
            </button>
          </div>

          <!-- Add Network Device Form -->
          <mat-card *ngIf="showAddNetwork" class="add-form-card">
            <mat-card-header><mat-card-title>New Network Device</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Name</mat-label>
                  <input matInput [(ngModel)]="newNetDev.name" placeholder="TOR-SW-01">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Vendor</mat-label>
                  <input matInput [(ngModel)]="newNetDev.vendor" placeholder="Arista">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Model</mat-label>
                  <input matInput [(ngModel)]="newNetDev.model" placeholder="7050SX3-48YC12">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Management IP</mat-label>
                  <input matInput [(ngModel)]="newNetDev.management_ip" placeholder="10.0.60.1">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Role</mat-label>
                  <mat-select [(ngModel)]="newNetDev.role" panelClass="tn-select-panel">
                    <mat-option value="tor">Top-of-Rack</mat-option>
                    <mat-option value="spine">Spine</mat-option>
                    <mat-option value="leaf">Leaf</mat-option>
                    <mat-option value="firewall">Firewall</mat-option>
                    <mat-option value="router">Router</mat-option>
                    <mat-option value="oob">Out-of-Band</mat-option>
                  </mat-select>
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Port Count</mat-label>
                  <input matInput type="number" [(ngModel)]="newNetDev.port_count">
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Firmware Version</mat-label>
                  <input matInput [(ngModel)]="newNetDev.firmware_version" placeholder="4.32.1F">
                </mat-form-field>
              </div>
            </mat-card-content>
            <mat-card-actions>
              <button mat-raised-button color="primary" (click)="createNetDevice()">Save</button>
              <button mat-button (click)="showAddNetwork = false">Cancel</button>
            </mat-card-actions>
          </mat-card>

          <!-- Network Summary -->
          <div class="summary-row" *ngIf="networkSummary">
            <mat-card class="stat-card">
              <mat-icon>router</mat-icon>
              <div class="stat-value">{{ networkSummary.total_devices }}</div>
              <div class="stat-label">Devices</div>
            </mat-card>
            <mat-card class="stat-card">
              <mat-icon>check_circle</mat-icon>
              <div class="stat-value">{{ networkSummary.active_devices }}</div>
              <div class="stat-label">Active</div>
            </mat-card>
          </div>

          <!-- Network Devices Table -->
          <mat-card>
            <mat-card-header><mat-card-title>Network Devices</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="table-wrap">
                <table mat-table [dataSource]="netDevices" class="full-width">
                  <ng-container matColumnDef="status">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let d">
                      <mat-icon [class]="d.is_active ? 'status-online' : 'status-offline'">
                        {{ d.is_active ? 'check_circle' : 'cancel' }}
                      </mat-icon>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="name">
                    <th mat-header-cell *matHeaderCellDef>Name</th>
                    <td mat-cell *matCellDef="let d">{{ d.name }}</td>
                  </ng-container>
                  <ng-container matColumnDef="vendor">
                    <th mat-header-cell *matHeaderCellDef>Vendor</th>
                    <td mat-cell *matCellDef="let d">{{ d.vendor }}</td>
                  </ng-container>
                  <ng-container matColumnDef="model">
                    <th mat-header-cell *matHeaderCellDef>Model</th>
                    <td mat-cell *matCellDef="let d">{{ d.model }}</td>
                  </ng-container>
                  <ng-container matColumnDef="role">
                    <th mat-header-cell *matHeaderCellDef>Role</th>
                    <td mat-cell *matCellDef="let d">
                      <mat-chip-set><mat-chip>{{ d.role | uppercase }}</mat-chip></mat-chip-set>
                    </td>
                  </ng-container>
                  <ng-container matColumnDef="ip">
                    <th mat-header-cell *matHeaderCellDef>Mgmt IP</th>
                    <td mat-cell *matCellDef="let d">{{ d.management_ip }}</td>
                  </ng-container>
                  <ng-container matColumnDef="ports">
                    <th mat-header-cell *matHeaderCellDef>Ports</th>
                    <td mat-cell *matCellDef="let d">{{ d.port_count }}</td>
                  </ng-container>
                  <ng-container matColumnDef="firmware">
                    <th mat-header-cell *matHeaderCellDef>Firmware</th>
                    <td mat-cell *matCellDef="let d">{{ d.firmware_version || '—' }}</td>
                  </ng-container>
                  <ng-container matColumnDef="actions">
                    <th mat-header-cell *matHeaderCellDef></th>
                    <td mat-cell *matCellDef="let d">
                      <button mat-icon-button matTooltip="Delete" color="warn" (click)="deleteNetDevice(d)"><mat-icon>delete</mat-icon></button>
                    </td>
                  </ng-container>
                  <tr mat-header-row *matHeaderRowDef="netCols"></tr>
                  <tr mat-row *matRowDef="let row; columns: netCols;"></tr>
                </table>
              </div>
              <p *ngIf="netDevices.length === 0" class="empty-state">No network devices registered.</p>
            </mat-card-content>
          </mat-card>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    .header-left { display: flex; align-items: center; gap: 16px; }
    h1 { margin: 0; font-size: 24px; color: var(--text-primary); }
    .subtitle { margin: 4px 0 0; color: var(--text-secondary); font-size: 14px; }
    .tab-icon { margin-right: 6px; font-size: 20px; width: 20px; height: 20px; }
    .tab-actions { display: flex; justify-content: flex-end; gap: 12px; margin: 16px 0; flex-wrap: wrap; }
    .summary-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin: 16px 0; }
    .stat-card { background: var(--bg-card); border: 1px solid var(--border); padding: 16px; text-align: center; }
    .add-form-card { margin-bottom: 16px; background: var(--bg-card); border: 1px solid var(--accent); }
    .full-width { width: 100%; }
    .status-online { color: #4caf50; }
    .status-offline { color: #f44336; }
    .primary-badge { color: #ffc107; font-size: 16px; width: 16px; height: 16px; vertical-align: middle; margin-left: 4px; }
    .empty-state { text-align: center; padding: 40px; color: var(--text-secondary); }
    .notice-card { margin: 16px 0; border: 1px solid var(--warning); background: color-mix(in srgb, var(--warning) 12%, var(--bg-card)); }
    .notice-content { display: flex; gap: 12px; align-items: flex-start; }
    .notice-content mat-icon { color: var(--warning); margin-top: 2px; }
    .notice-content p { margin: 4px 0 0; color: var(--text-secondary); }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
    mat-card { background: var(--bg-card); border: 1px solid var(--border); }
  `],
})
export class InfrastructureComponent implements OnInit {
  /* ── Compute state ─────────────────────────────────────────── */
  connections: HypervisorConnection[] = [];
  allNodes: HypervisorNode[] = [];
  hvSummary: HypervisorSummary | null = null;
  showAddConn = false;
  discoveringAll = false;
  connCols = ['status', 'name', 'type', 'host', 'actions'];
  nodeCols = ['status', 'node_name', 'ip', 'cpu', 'memory', 'storage', 'vms'];
  newConn = {
    name: '', hypervisor_type: 'proxmox' as const, host: '', port: 8006,
    username: '', password: '', verify_ssl: false, is_primary: false,
  };

  /* ── Storage state ─────────────────────────────────────────── */
  appliances: StorageAppliance[] = [];
  storageSummary: StorageSummary | null = null;
  showAddStorage = false;
  applianceCols = ['status', 'name', 'vendor', 'model', 'ip', 'protocol', 'capacity', 'actions'];
  newAppliance = {
    name: '', vendor: '', model: '', management_ip: '',
    protocol: 'nfs' as StorageProtocol, raw_capacity_tb: 0, usable_capacity_tb: 0,
  };

  /* ── Network state ─────────────────────────────────────────── */
  netDevices: NetworkDevice[] = [];
  networkSummary: NetworkSummary | null = null;
  showAddNetwork = false;
  netCols = ['status', 'name', 'vendor', 'model', 'role', 'ip', 'ports', 'firmware', 'actions'];
  newNetDev = {
    name: '', vendor: '', model: '', management_ip: '',
    role: 'tor' as NetworkDeviceRole, port_count: 48, firmware_version: '',
  };

  constructor(private http: HttpClient, private snack: MatSnackBar) {}

  ngOnInit(): void {
    this.loadConnections();
    this.loadHvSummary();
    this.loadAppliances();
    this.loadStorageSummary();
    this.loadNetDevices();
    this.loadNetSummary();
  }

  /* ── Compute helpers ───────────────────────────────────────── */
  loadConnections(): void {
    this.http.get<HypervisorConnection[]>('/api/hypervisors/connections').subscribe({
      next: c => {
        this.connections = c;
        this.loadAllNodes();
      },
      error: () => this.snack.open('Failed to load connections', 'OK', { duration: 3000 }),
    });
  }

  /**
   * Load nodes from all connections and deduplicate by node_name.
   * In a Proxmox cluster, both connections see the same physical nodes.
   * Without dedup the UI would show each node twice with swapped IPs.
   */
  loadAllNodes(): void {
    const nodeMap = new Map<string, HypervisorNode>();
    let pending = this.connections.length;
    if (pending === 0) { this.allNodes = []; return; }

    for (const c of this.connections) {
      this.http.get<HypervisorNode[]>(`/api/hypervisors/connections/${c.id}/nodes`).subscribe({
        next: nodes => {
          for (const n of nodes) {
            // Keep the latest version seen (identical after backend dedup)
            nodeMap.set(n.node_name, n);
          }
          pending--;
          if (pending <= 0) {
            this.allNodes = Array.from(nodeMap.values());
          }
        },
        error: () => {
          pending--;
          if (pending <= 0) {
            this.allNodes = Array.from(nodeMap.values());
          }
        },
      });
    }
  }

  loadHvSummary(): void {
    this.http.get<HypervisorSummary>('/api/hypervisors/summary').subscribe({
      next: s => this.hvSummary = s,
      error: () => {},
    });
  }
  createConnection(): void {
    this.http.post('/api/hypervisors/connections', this.newConn).subscribe({
      next: () => { this.showAddConn = false; this.loadConnections(); this.loadHvSummary(); },
      error: e => this.snack.open('Failed to create connection: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }
  testConnection(c: HypervisorConnection): void {
    this.snack.open('Testing ' + c.name + '...', '', { duration: 2000 });
    this.http.post<any>(`/api/hypervisors/connections/${c.id}/test`, {}).subscribe({
      next: r => {
        this.snack.open(r.message, 'OK', { duration: 5000 });
        this.loadConnections();
      },
      error: e => this.snack.open('Test failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }
  discoverNodes(c: HypervisorConnection): void {
    this.snack.open('Discovering nodes on ' + c.name + '...', '', { duration: 2000 });
    this.http.post<any>(`/api/hypervisors/connections/${c.id}/discover`, {}).subscribe({
      next: r => {
        this.snack.open(r.message, 'OK', { duration: 5000 });
        this.loadConnections();
        this.loadHvSummary();
      },
      error: e => this.snack.open('Discovery failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }

  async discoverAllNodes(): Promise<void> {
    if (this.connections.length === 0 || this.discoveringAll) return;

    this.discoveringAll = true;
    this.snack.open(`Starting discovery across ${this.connections.length} connection(s)...`, '', { duration: 2500 });

    try {
      const results = await Promise.allSettled(
        this.connections.map(c => firstValueFrom(this.http.post<any>(`/api/hypervisors/connections/${c.id}/discover`, {})))
      );

      const success = results.filter(r => r.status === 'fulfilled').length;
      const failed = results.length - success;

      if (failed === 0) {
        this.snack.open(`Discovery complete on ${success} connection(s).`, 'OK', { duration: 5000 });
      } else {
        this.snack.open(`Discovery finished: ${success} succeeded, ${failed} failed.`, 'OK', { duration: 6000 });
      }
    } finally {
      this.discoveringAll = false;
      this.loadConnections();
      this.loadHvSummary();
    }
  }
  setPrimary(c: HypervisorConnection): void {
    this.http.post<any>(`/api/hypervisors/connections/${c.id}/set-primary`, {}).subscribe({
      next: () => this.loadConnections(),
      error: () => this.snack.open('Failed to set primary', 'OK', { duration: 3000 }),
    });
  }
  deleteConnection(c: HypervisorConnection): void {
    if (!confirm(`Delete connection "${c.name}"?`)) return;
    this.http.delete(`/api/hypervisors/connections/${c.id}`).subscribe({
      next: () => { this.loadConnections(); this.loadHvSummary(); },
      error: () => this.snack.open('Failed to delete', 'OK', { duration: 3000 }),
    });
  }

  /* ── Storage helpers ───────────────────────────────────────── */
  loadAppliances(): void {
    this.http.get<StorageAppliance[]>('/api/storage/appliances').subscribe({
      next: a => this.appliances = a,
      error: () => this.snack.open('Failed to load storage appliances', 'OK', { duration: 3000 }),
    });
  }
  loadStorageSummary(): void {
    this.http.get<StorageSummary>('/api/storage/summary').subscribe({
      next: s => this.storageSummary = s,
      error: () => {},
    });
  }
  createAppliance(): void {
    this.http.post('/api/storage/appliances', this.newAppliance).subscribe({
      next: () => { this.showAddStorage = false; this.loadAppliances(); this.loadStorageSummary(); },
      error: e => this.snack.open('Failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }
  deleteAppliance(a: StorageAppliance): void {
    if (!confirm(`Delete appliance "${a.name}"?`)) return;
    this.http.delete(`/api/storage/appliances/${a.id}`).subscribe({
      next: () => { this.loadAppliances(); this.loadStorageSummary(); },
      error: () => this.snack.open('Failed to delete', 'OK', { duration: 3000 }),
    });
  }

  /* ── Network helpers ───────────────────────────────────────── */
  loadNetDevices(): void {
    this.http.get<NetworkDevice[]>('/api/network-devices/').subscribe({
      next: d => this.netDevices = d,
      error: () => this.snack.open('Failed to load network devices', 'OK', { duration: 3000 }),
    });
  }
  loadNetSummary(): void {
    this.http.get<NetworkSummary>('/api/network-devices/summary').subscribe({
      next: s => this.networkSummary = s,
      error: () => {},
    });
  }
  createNetDevice(): void {
    this.http.post('/api/network-devices/', this.newNetDev).subscribe({
      next: () => { this.showAddNetwork = false; this.loadNetDevices(); this.loadNetSummary(); },
      error: e => this.snack.open('Failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }
  deleteNetDevice(d: NetworkDevice): void {
    if (!confirm(`Delete device "${d.name}"?`)) return;
    this.http.delete(`/api/network-devices/${d.id}`).subscribe({
      next: () => { this.loadNetDevices(); this.loadNetSummary(); },
      error: () => this.snack.open('Failed to delete', 'OK', { duration: 3000 }),
    });
  }
}