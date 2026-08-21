import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { ApiService } from '@core/services/api.service';

interface ExternalPlatform {
  id: string;
  name: string;
  slug: string;
  platform_type: string;
  base_url: string;
  auth_type: string;
  is_active: boolean;
  last_sync_at: string | null;
}

@Component({
  selector: 'tn-integrations',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatTabsModule, MatTableModule, MatFormFieldModule, MatInputModule,
    MatSelectModule, MatChipsModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">hub</mat-icon>
          <div>
            <h1>Integrations</h1>
            <p class="subtitle">Connect external training platforms, LTI tools, and learning record stores.</p>
          </div>
        </div>
      </div>

      <mat-tab-group>
        <mat-tab label="Connected Platforms">
          <div class="tab-content mt-2">
            <div class="page-header">
              <button mat-raised-button color="primary" (click)="showAdd = !showAdd">
                <mat-icon>add</mat-icon> Add Platform
              </button>
            </div>

            @if (showAdd || editingPlatformId) {
              <mat-card class="add-form mt-2">
                <mat-card-content>
                  <h3>{{ editingPlatformId ? 'Edit Platform' : 'Register External Platform' }}</h3>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Name</mat-label>
                      <input matInput [(ngModel)]="newPlatform.name" placeholder="e.g. Moodle LMS">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Slug</mat-label>
                      <input matInput [(ngModel)]="newPlatform.slug" placeholder="e.g. moodle">
                    </mat-form-field>
                  </div>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Base URL</mat-label>
                      <input matInput [(ngModel)]="newPlatform.base_url" placeholder="https://moodle.example.com">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Auth Type</mat-label>
                      <mat-select [(value)]="newPlatform.auth_type" panelClass="tn-select-panel">
                        <mat-option value="lti13">LTI 1.3</mat-option>
                        <mat-option value="oauth2">OAuth 2.0</mat-option>
                        <mat-option value="api_key">API Key</mat-option>
                        <mat-option value="saml">SAML</mat-option>
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Platform Type</mat-label>
                      <mat-select [(value)]="newPlatform.platform_type" panelClass="tn-select-panel">
                        <mat-option value="moodle">Moodle</mat-option>
                        <mat-option value="immersive_labs">Immersive Labs</mat-option>
                        <mat-option value="offsec">OffSec</mat-option>
                        <mat-option value="lti_generic">Generic LTI</mat-option>
                        <mat-option value="custom">Custom</mat-option>
                      </mat-select>
                    </mat-form-field>
                  </div>
                  <button mat-raised-button color="primary"
                    [disabled]="platformSaving"
                    (click)="editingPlatformId ? updatePlatform() : addPlatform()">
                    {{ platformSaving ? 'Saving...' : 'Save' }}
                  </button>
                  <button mat-button [disabled]="platformSaving"
                    (click)="editingPlatformId ? cancelPlatformEdit() : showAdd = false">Cancel</button>
                </mat-card-content>
              </mat-card>
            }

            <div class="card-grid mt-2">
              @for (p of platforms(); track p.id) {
                <mat-card class="platform-card">
                  <mat-card-header>
                    <mat-icon mat-card-avatar [color]="p.is_active ? 'primary' : 'warn'">
                      {{ platformIcon(p.platform_type) }}
                    </mat-icon>
                    <mat-card-title>{{ p.name }}</mat-card-title>
                    <mat-card-subtitle>{{ p.base_url }}</mat-card-subtitle>
                  </mat-card-header>
                  <mat-card-content>
                    <div class="chip-row">
                      <span class="tn-chip">{{ p.auth_type }}</span>
                      <span class="tn-chip">{{ p.platform_type }}</span>
                      <span class="tn-chip" [class.active]="p.is_active" [class.inactive]="!p.is_active">
                        {{ p.is_active ? 'Active' : 'Inactive' }}
                      </span>
                    </div>
                    @if (p.last_sync_at) {
                      <p class="sync-info">Last sync: {{ p.last_sync_at | date:'medium' }}</p>
                    }
                  </mat-card-content>
                  <mat-card-actions align="end">
                    <button mat-button (click)="testConnection(p)">
                      <mat-icon>network_check</mat-icon> Test
                    </button>
                    <button mat-button (click)="startEditPlatform(p)">
                      <mat-icon>edit</mat-icon> Edit
                    </button>
                    <button mat-button color="warn" (click)="deletePlatform(p)">
                      <mat-icon>delete</mat-icon> Remove
                    </button>
                  </mat-card-actions>
                </mat-card>
              } @empty {
                <mat-card>
                  <mat-card-content>
                    <div style="text-align: center; padding: 32px;">
                      <mat-icon style="font-size: 48px; width: 48px; height: 48px; color: var(--text-secondary)">hub</mat-icon>
                      <h3>No Platforms Connected</h3>
                      <p>Connect Moodle, Immersive Labs, OffSec, or any LTI 1.3 tool provider.</p>
                    </div>
                  </mat-card-content>
                </mat-card>
              }
            </div>
          </div>
        </mat-tab>

        <mat-tab label="LTI 1.3 Config">
          <div class="tab-content mt-2">
            <mat-card>
              <mat-card-header>
                <mat-icon mat-card-avatar>settings</mat-icon>
                <mat-card-title>TrueNorth LTI 1.3 Tool Configuration</mat-card-title>
                <mat-card-subtitle>Provide these values when registering TrueNorth as a tool in external platforms</mat-card-subtitle>
              </mat-card-header>
              <mat-card-content>
                <table class="config-table">
                  <tr><td>OIDC Login URL</td><td><code>/api/v1/lti/login</code></td></tr>
                  <tr><td>Launch URL</td><td><code>/api/v1/lti/launch</code></td></tr>
                  <tr><td>JWKS URL</td><td><code>/api/v1/lti/jwks</code></td></tr>
                  <tr><td>Deep Linking URL</td><td><code>/api/v1/lti/deeplink</code></td></tr>
                  <tr><td>Grade Callback</td><td><code>/api/v1/lti/grades</code></td></tr>
                </table>
              </mat-card-content>
            </mat-card>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 16px; }
    .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }
    .form-row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }
    .form-row mat-form-field { flex: 1; min-width: 200px; }
    .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
    .tn-chip { background: var(--bg-surface); padding: 2px 10px; border-radius: 12px; font-size: 12px; }
    .tn-chip.active { background: var(--success, #22c55e); color: white; }
    .tn-chip.inactive { background: var(--alert, #ef4444); color: white; }
    .sync-info { font-size: 12px; color: var(--text-secondary); margin-top: 8px; }
    .tab-content { padding: 16px 0; }
    .page-header { display: flex; justify-content: flex-end; }
    .mt-2 { margin-top: 16px; }
    .config-table { width: 100%; border-collapse: collapse; margin-top: 16px; }
    .config-table td { padding: 8px 12px; border-bottom: 1px solid var(--border, #333); }
    .config-table td:first-child { font-weight: 600; width: 200px; }
    code { background: var(--bg-surface); border: 1px solid var(--border); padding: 2px 6px; border-radius: var(--radius-sm); font-family: var(--font-mono); }
  `],
})
export class IntegrationsComponent implements OnInit {
  platforms = signal<ExternalPlatform[]>([]);
  showAdd = false;
  editingPlatformId: string | null = null;
  platformSaving = false;
  newPlatform: any = { name: '', slug: '', base_url: '', auth_type: 'lti13', platform_type: 'moodle' };

  constructor(private api: ApiService) {}

  ngOnInit() {
    this.loadPlatforms();
  }

  platformIcon(type: string): string {
    const icons: Record<string, string> = {
      moodle: 'school', immersive_labs: 'security', offsec: 'hacking',
      lti_generic: 'extension', custom: 'settings',
    };
    return icons[type] || 'hub';
  }

  loadPlatforms() {
    this.api.get<ExternalPlatform[]>('/integrations/platforms').subscribe({
      next: p => this.platforms.set(p || []),
      error: () => this.platforms.set([]),
    });
  }

  addPlatform() {
    this.api.post('/integrations/platforms', this.newPlatform).subscribe({
      next: () => { this.showAdd = false; this.loadPlatforms(); },
      error: (err: any) => alert(err.error?.detail || 'Failed to add platform'),
    });
  }

  startEditPlatform(p: ExternalPlatform) {
    this.showAdd = false;
    this.editingPlatformId = p.id;
    this.newPlatform = {
      name: p.name,
      slug: p.slug,
      base_url: p.base_url,
      auth_type: p.auth_type,
      platform_type: p.platform_type,
    };
  }

  updatePlatform() {
    if (!this.editingPlatformId) return;
    this.platformSaving = true;
    this.api.patch('/integrations/platforms/' + this.editingPlatformId, this.newPlatform).subscribe({
      next: () => {
        this.cancelPlatformEdit();
        this.loadPlatforms();
      },
      error: (err: any) => {
        this.platformSaving = false;
        alert(err.error?.detail || 'Failed to update platform');
      },
    });
  }

  cancelPlatformEdit() {
    this.editingPlatformId = null;
    this.platformSaving = false;
    this.newPlatform = { name: '', slug: '', base_url: '', auth_type: 'lti13', platform_type: 'moodle' };
  }

  testConnection(p: ExternalPlatform) {
    this.api.post(`/integrations/platforms/${p.id}/test`, {}).subscribe({
      next: (res: any) => alert(`Connection ${res.reachable ? 'OK' : 'Failed'}: ${res.error || 'Status ' + res.status_code}`),
      error: (err: any) => alert(`Test failed: ${err.error?.detail || err.message}`),
    });
  }

  deletePlatform(p: ExternalPlatform) {
    if (!confirm(`Remove ${p.name}?`)) return;
    this.api.delete(`/integrations/platforms/${p.id}`).subscribe({
      next: () => this.loadPlatforms(),
      error: (err: any) => alert(err.error?.detail || 'Delete failed'),
    });
  }
}
