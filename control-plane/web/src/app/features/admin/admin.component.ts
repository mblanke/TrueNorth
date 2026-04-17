import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatTableModule } from '@angular/material/table';
import { MatTabsModule } from '@angular/material/tabs';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Tenant, HealthResponse } from '@core/models';

interface AuditEntry {
  id: string;
  user_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail: string | null;
  timestamp: string;
}

@Component({
  selector: 'tn-admin',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatTableModule,
    MatTabsModule, MatChipsModule, MatTooltipModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">admin_panel_settings</mat-icon>
          <div>
            <h1>Administration</h1>
            <p class="subtitle">System health, tenants, audit log, and external services</p>
          </div>
        </div>
      </div>

      <mat-tab-group>
        <mat-tab label="System Health">
          <div class="tab-content">
            @if (health()) {
              <div class="stats-row mt-2">
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon [style.color]="health()!.status === 'ok' ? 'var(--success)' : 'var(--alert)'">
                      {{ health()!.status === 'ok' ? 'check_circle' : 'error' }}
                    </mat-icon>
                    <div class="stat-value">{{ health()!.status | uppercase }}</div>
                    <div class="stat-label">API Status</div>
                  </mat-card-content>
                </mat-card>
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon [style.color]="health()!.db ? 'var(--success)' : 'var(--alert)'">storage</mat-icon>
                    <div class="stat-value">{{ health()!.db ? 'Connected' : 'Down' }}</div>
                    <div class="stat-label">Database</div>
                  </mat-card-content>
                </mat-card>
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon>info</mat-icon>
                    <div class="stat-value">{{ health()!.version }}</div>
                    <div class="stat-label">Version</div>
                  </mat-card-content>
                </mat-card>
              </div>
            }
          </div>
        </mat-tab>

        <mat-tab label="Tenants">
          <div class="tab-content">
            <div class="page-header mt-2">
              <h2>Tenants</h2>
              <button mat-raised-button color="primary" (click)="showCreateTenant = !showCreateTenant">
                <mat-icon>add</mat-icon> New Tenant
              </button>
            </div>
            @if (showCreateTenant) {
              <mat-card class="mt-1">
                <mat-card-content>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Name</mat-label>
                      <input matInput [(ngModel)]="tenantForm.name">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Slug</mat-label>
                      <input matInput [(ngModel)]="tenantForm.slug">
                    </mat-form-field>
                  </div>
                  <button mat-raised-button color="primary" (click)="createTenant()">Create</button>
                </mat-card-content>
              </mat-card>
            }

            <!-- INLINE EDIT FORM -->
            @if (editingTenantId) {
              <mat-card class="mt-1">
                <mat-card-header>
                  <mat-card-title>Edit Tenant</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Name</mat-label>
                      <input matInput [(ngModel)]="tenantEditForm.name">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Slug</mat-label>
                      <input matInput [(ngModel)]="tenantEditForm.slug">
                    </mat-form-field>
                  </div>
                  <div class="form-actions">
                    <button mat-raised-button color="primary" (click)="updateTenant()" [disabled]="!tenantEditForm.name || tenantSaving">
                      <mat-icon>save</mat-icon> Save
                    </button>
                    <button mat-button (click)="cancelTenantEdit()">Cancel</button>
                  </div>
                </mat-card-content>
              </mat-card>
            }

            <table mat-table [dataSource]="tenants()" class="mt-2 full-width">
              <ng-container matColumnDef="name"><th mat-header-cell *matHeaderCellDef>Name</th><td mat-cell *matCellDef="let t">{{ t.name }}</td></ng-container>
              <ng-container matColumnDef="slug"><th mat-header-cell *matHeaderCellDef>Slug</th><td mat-cell *matCellDef="let t">{{ t.slug }}</td></ng-container>
              <ng-container matColumnDef="id"><th mat-header-cell *matHeaderCellDef>ID</th><td mat-cell *matCellDef="let t">{{ t.id }}</td></ng-container>
              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef>Actions</th>
                <td mat-cell *matCellDef="let t">
                  <button mat-icon-button matTooltip="Edit" (click)="startEditTenant(t)">
                    <mat-icon>edit</mat-icon>
                  </button>
                  <button mat-icon-button matTooltip="Delete" color="warn" (click)="confirmDeleteTenant(t)">
                    <mat-icon>delete</mat-icon>
                  </button>
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="tenantColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: tenantColumns"
                  [class.selected-row]="row.id === editingTenantId"></tr>
            </table>

            <!-- DELETE CONFIRMATION -->
            @if (tenantDeleteTarget) {
              <div class="confirm-overlay" (click)="tenantDeleteTarget = null" (keyup.escape)="tenantDeleteTarget = null" tabindex="0" role="dialog">
                <mat-card class="confirm-dialog" (click)="$event.stopPropagation()">
                  <mat-card-header>
                    <mat-card-title>Delete Tenant</mat-card-title>
                  </mat-card-header>
                  <mat-card-content>
                    <p>Are you sure you want to delete <strong>{{ tenantDeleteTarget.name }}</strong>?</p>
                    <p class="warn-text">This action cannot be undone.</p>
                  </mat-card-content>
                  <mat-card-actions align="end">
                    <button mat-button (click)="tenantDeleteTarget = null">Cancel</button>
                    <button mat-raised-button color="warn" (click)="doDeleteTenant()" [disabled]="tenantSaving">
                      <mat-icon>delete</mat-icon> Delete
                    </button>
                  </mat-card-actions>
                </mat-card>
              </div>
            }
          </div>
        </mat-tab>

        <mat-tab label="Audit Log">
          <div class="tab-content">
            <div class="page-header mt-2">
              <h2>Audit Log</h2>
              <button mat-raised-button (click)="loadAuditLog()">
                <mat-icon>refresh</mat-icon> Refresh
              </button>
            </div>
            <table mat-table [dataSource]="auditLog()" class="mt-2 full-width">
              <ng-container matColumnDef="timestamp">
                <th mat-header-cell *matHeaderCellDef>Time</th>
                <td mat-cell *matCellDef="let e">{{ e.timestamp | date:'short' }}</td>
              </ng-container>
              <ng-container matColumnDef="action">
                <th mat-header-cell *matHeaderCellDef>Action</th>
                <td mat-cell *matCellDef="let e">
                  <span class="action-chip" [class]="'action-' + e.action">{{ e.action }}</span>
                </td>
              </ng-container>
              <ng-container matColumnDef="resource_type">
                <th mat-header-cell *matHeaderCellDef>Resource</th>
                <td mat-cell *matCellDef="let e">{{ e.resource_type }}</td>
              </ng-container>
              <ng-container matColumnDef="resource_id">
                <th mat-header-cell *matHeaderCellDef>Resource ID</th>
                <td mat-cell *matCellDef="let e" class="mono">{{ e.resource_id | slice:0:8 }}...</td>
              </ng-container>
              <ng-container matColumnDef="user_id">
                <th mat-header-cell *matHeaderCellDef>User ID</th>
                <td mat-cell *matCellDef="let e" class="mono">{{ e.user_id | slice:0:8 }}...</td>
              </ng-container>
              <ng-container matColumnDef="detail">
                <th mat-header-cell *matHeaderCellDef>Detail</th>
                <td mat-cell *matCellDef="let e">{{ e.detail || '-' }}</td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="auditColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: auditColumns"></tr>
            </table>
            <p *ngIf="auditLog().length === 0" class="empty-state">No audit entries yet.</p>
          </div>
        </mat-tab>

        <mat-tab label="Services">
          <div class="tab-content">
            <h2 class="mt-2">External Services</h2>
            <div class="card-grid">
              <mat-card>
                <mat-card-header><mat-card-title>Keycloak</mat-card-title></mat-card-header>
                <mat-card-actions><a mat-button href="http://localhost:8180" target="_blank">Open <mat-icon>open_in_new</mat-icon></a></mat-card-actions>
              </mat-card>
              <mat-card>
                <mat-card-header><mat-card-title>MinIO Console</mat-card-title></mat-card-header>
                <mat-card-actions><a mat-button href="http://localhost:9001" target="_blank">Open <mat-icon>open_in_new</mat-icon></a></mat-card-actions>
              </mat-card>
              <mat-card>
                <mat-card-header><mat-card-title>OpenSearch Dashboards</mat-card-title></mat-card-header>
                <mat-card-actions><a mat-button href="http://localhost:5602" target="_blank">Open <mat-icon>open_in_new</mat-icon></a></mat-card-actions>
              </mat-card>
              <mat-card>
                <mat-card-header><mat-card-title>AI Orchestrator</mat-card-title></mat-card-header>
                <mat-card-actions><a mat-button href="http://localhost:6000/docs" target="_blank">Open <mat-icon>open_in_new</mat-icon></a></mat-card-actions>
              </mat-card>
            </div>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`.page-header { display: flex; justify-content: space-between; align-items: center; }
    .full-width { width: 100%; }
    .tab-content { padding: 16px 0; }
    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stats-row mat-card { min-width: 200px; text-align: center; padding: 16px; }
    .stats-row mat-icon { font-size: 32px; width: 32px; height: 32px; }
    mat-card-content { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
    mat-card-content mat-form-field { flex: 1; min-width: 200px; }
    .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
    .action-chip { padding: 2px 10px; border-radius: 12px; font-size: 12px; background: var(--surface-light, #1e293b); }
    .action-create { background: #22c55e33; color: #4ade80; }
    .action-update { background: #3b82f633; color: #60a5fa; }
    .action-delete { background: #ef444433; color: #f87171; }
    .mono { font-family: monospace; font-size: 12px; color: var(--text-secondary); }
    .empty-state { text-align: center; padding: 40px; color: var(--text-secondary); }
    table th, table td { color: var(--text-primary) !important; }
    table { background: transparent !important; }
    .form-actions { display: flex; gap: 8px; }
    .selected-row { background: rgba(0, 188, 212, 0.08); }
    .confirm-overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,0.5);
      display: flex; align-items: center; justify-content: center; z-index: 1000;
    }
    .confirm-dialog { max-width: 420px; width: 100%; }
    .warn-text { color: #ef5350; font-size: 0.85em; }`],
})
export class AdminComponent implements OnInit {
  health = signal<HealthResponse | null>(null);
  tenants = signal<Tenant[]>([]);
  auditLog = signal<AuditEntry[]>([]);
  showCreateTenant = false;
  tenantForm = { name: '', slug: '' };
  tenantColumns = ['name', 'slug', 'id', 'actions'];
  auditColumns = ['timestamp', 'action', 'resource_type', 'resource_id', 'user_id', 'detail'];

  // ── Tenant edit / delete state ─────────────────────────
  editingTenantId: string | null = null;
  tenantEditForm = { name: '', slug: '' };
  tenantDeleteTarget: Tenant | null = null;
  tenantSaving = false;

  constructor(private api: ApiService, private notify: NotificationService) {}

  ngOnInit(): void {
    this.api.health().subscribe(h => this.health.set(h));
    this.loadTenants();
    this.loadAuditLog();
  }

  loadTenants(): void {
    this.api.listTenants().subscribe(t => this.tenants.set(t));
  }

  loadAuditLog(): void {
    this.api.listAuditLog().subscribe({
      next: (entries: any) => this.auditLog.set(entries || []),
      error: () => this.auditLog.set([]),
    });
  }

  createTenant(): void {
    this.api.createTenant(this.tenantForm).subscribe({
      next: () => {
        this.notify.success('Tenant created');
        this.loadTenants();
        this.showCreateTenant = false;
        this.tenantForm = { name: '', slug: '' };
      },
      error: () => this.notify.error('Failed'),
    });
  }

  // ── Tenant Edit ────────────────────────────────────────
  startEditTenant(t: Tenant): void {
    this.editingTenantId = t.id;
    this.tenantEditForm = { name: t.name, slug: t.slug };
  }

  updateTenant(): void {
    if (!this.editingTenantId) return;
    this.tenantSaving = true;
    this.api.updateTenant(this.editingTenantId, this.tenantEditForm).subscribe({
      next: () => {
        this.notify.success('Tenant updated');
        this.loadTenants();
        this.cancelTenantEdit();
      },
      error: () => { this.notify.error('Update failed'); this.tenantSaving = false; },
    });
  }

  cancelTenantEdit(): void {
    this.editingTenantId = null;
    this.tenantSaving = false;
    this.tenantEditForm = { name: '', slug: '' };
  }

  // ── Tenant Delete ──────────────────────────────────────
  confirmDeleteTenant(t: Tenant): void {
    this.tenantDeleteTarget = t;
  }

  doDeleteTenant(): void {
    if (!this.tenantDeleteTarget) return;
    const deletedId = this.tenantDeleteTarget.id;
    this.tenantSaving = true;
    this.api.deleteTenant(deletedId).subscribe({
      next: () => {
        this.notify.success('Tenant deleted');
        this.tenantDeleteTarget = null;
        this.tenantSaving = false;
        if (this.editingTenantId === deletedId) this.cancelTenantEdit();
        this.loadTenants();
      },
      error: () => { this.notify.error('Delete failed'); this.tenantSaving = false; },
    });
  }
}
