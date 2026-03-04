import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { HttpClient } from '@angular/common/http';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';

interface AIBackend {
  id: string;
  name: string;
  backend_type: string;
  base_url: string;
  is_active: boolean;
  is_primary: boolean;
  max_concurrent: number;
  timeout_seconds: number;
  notes: string | null;
  created_at: string;
}

interface AIFleetNode {
  id: string;
  backend_id: string;
  node_name: string;
  url: string;
  status: string;
  gpu_model: string | null;
  gpu_vram_gb: number | null;
  loaded_models: string | null;
  current_requests: number;
  max_requests: number;
}

interface AIModelRoute {
  id: string;
  model_pattern: string;
  backend_id: string;
  priority: number;
  tags: string | null;
  is_active: boolean;
}

interface AIFleetSummary {
  total_backends: number;
  active_backends: number;
  total_nodes: number;
  online_nodes: number;
  total_gpu_vram_gb: number;
  active_requests: number;
  model_routes: number;
  by_backend_type: Record<string, number>;
}

@Component({
  selector: 'app-ai-orchestrator',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatTabsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatTableModule, MatChipsModule, MatFormFieldModule,
    MatInputModule, MatSelectModule, MatTooltipModule, MatProgressBarModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">psychology</mat-icon>
          <div>
            <h1>AI Orchestrator</h1>
            <p class="subtitle">Backend providers, GPU fleet, model routing, and inference playground</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="showAddBackend = !showAddBackend">
          <mat-icon>add</mat-icon> Add Backend
        </button>
      </div>

      <!-- Summary -->
      <div class="summary-row" *ngIf="summary">
        <mat-card class="stat-card">
          <mat-icon>hub</mat-icon>
          <div class="stat-value">{{ summary.total_backends }}</div>
          <div class="stat-label">Backends</div>
        </mat-card>
        <mat-card class="stat-card">
          <mat-icon>developer_board</mat-icon>
          <div class="stat-value">{{ summary.total_nodes }}</div>
          <div class="stat-label">GPU Nodes</div>
        </mat-card>
        <mat-card class="stat-card">
          <mat-icon>memory</mat-icon>
          <div class="stat-value">{{ summary.total_gpu_vram_gb | number:'1.0-0' }} GB</div>
          <div class="stat-label">Total VRAM</div>
        </mat-card>
        <mat-card class="stat-card">
          <mat-icon>trending_up</mat-icon>
          <div class="stat-value">{{ summary.active_requests }}</div>
          <div class="stat-label">Active Requests</div>
        </mat-card>
        <mat-card class="stat-card">
          <mat-icon>alt_route</mat-icon>
          <div class="stat-value">{{ summary.model_routes }}</div>
          <div class="stat-label">Model Routes</div>
        </mat-card>
      </div>

      <mat-tab-group>
        <!-- Backends Tab -->
        <mat-tab label="Backends">
          <mat-card *ngIf="showAddBackend" class="add-form-card">
            <mat-card-header><mat-card-title>New AI Backend</mat-card-title></mat-card-header>
            <mat-card-content>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Name</mat-label>
                  <input matInput [(ngModel)]="newBackend.name" placeholder="Ollama Fleet">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Type</mat-label>
                  <mat-select [(ngModel)]="newBackend.backend_type" panelClass="tn-select-panel">
                    <mat-option value="ollama">Ollama</mat-option>
                    <mat-option value="openai">OpenAI</mat-option>
                    <mat-option value="anthropic">Anthropic</mat-option>
                    <mat-option value="azure_openai">Azure OpenAI</mat-option>
                    <mat-option value="mock">Mock</mat-option>
                  </mat-select>
                </mat-form-field>
              </div>
              <div class="form-row">
                <mat-form-field appearance="outline">
                  <mat-label>Base URL</mat-label>
                  <input matInput [(ngModel)]="newBackend.base_url" placeholder="https://ai.guapo613.beer">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>API Key</mat-label>
                  <input matInput type="password" [(ngModel)]="newBackend.api_key">
                </mat-form-field>
              </div>
            </mat-card-content>
            <mat-card-actions>
              <button mat-raised-button color="primary" (click)="createBackend()">Save</button>
              <button mat-button (click)="showAddBackend = false">Cancel</button>
            </mat-card-actions>
          </mat-card>

          <table mat-table [dataSource]="backends" class="full-width section-table">
            <ng-container matColumnDef="status">
              <th mat-header-cell *matHeaderCellDef></th>
              <td mat-cell *matCellDef="let b">
                <mat-icon [class]="b.is_active ? 'status-online' : 'status-offline'">
                  {{ b.is_active ? 'check_circle' : 'cancel' }}
                </mat-icon>
              </td>
            </ng-container>
            <ng-container matColumnDef="name">
              <th mat-header-cell *matHeaderCellDef>Name</th>
              <td mat-cell *matCellDef="let b">
                {{ b.name }}
                <mat-icon *ngIf="b.is_primary" class="primary-badge" matTooltip="Primary">star</mat-icon>
              </td>
            </ng-container>
            <ng-container matColumnDef="type">
              <th mat-header-cell *matHeaderCellDef>Type</th>
              <td mat-cell *matCellDef="let b">{{ b.backend_type | uppercase }}</td>
            </ng-container>
            <ng-container matColumnDef="url">
              <th mat-header-cell *matHeaderCellDef>URL</th>
              <td mat-cell *matCellDef="let b">{{ b.base_url }}</td>
            </ng-container>
            <ng-container matColumnDef="actions">
              <th mat-header-cell *matHeaderCellDef>Actions</th>
              <td mat-cell *matCellDef="let b">
                <button mat-icon-button matTooltip="Set Primary" (click)="setPrimary(b)" [disabled]="b.is_primary">
                  <mat-icon>star_border</mat-icon>
                </button>
                <button mat-icon-button matTooltip="Delete" color="warn" (click)="deleteBackend(b)">
                  <mat-icon>delete</mat-icon>
                </button>
              </td>
            </ng-container>
            <tr mat-header-row *matHeaderRowDef="backendColumns"></tr>
            <tr mat-row *matRowDef="let row; columns: backendColumns;"></tr>
          </table>
        </mat-tab>

        <!-- Model Routes Tab -->
        <mat-tab label="Model Routes">
          <div class="tab-content">
            <div class="tab-toolbar">
              <span class="toolbar-spacer"></span>
              <button mat-raised-button color="primary" (click)="showAddRoute = !showAddRoute">
                <mat-icon>add</mat-icon> {{ showAddRoute ? 'Cancel' : 'Add Route' }}
              </button>
            </div>

            <mat-card *ngIf="showAddRoute" class="add-form-card">
              <mat-card-header><mat-card-title>New Model Route</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Model Pattern</mat-label>
                    <input matInput [(ngModel)]="newRoute.model_pattern" placeholder="llama3*">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Backend</mat-label>
                    <mat-select [(ngModel)]="newRoute.backend_id" panelClass="tn-select-panel">
                      <mat-option *ngFor="let b of backends" [value]="b.id">{{ b.name }} ({{ b.backend_type }})</mat-option>
                    </mat-select>
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Priority</mat-label>
                    <input matInput type="number" [(ngModel)]="newRoute.priority" placeholder="0">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Tags</mat-label>
                    <input matInput [(ngModel)]="newRoute.tags" placeholder="production,fast">
                  </mat-form-field>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="createRoute()" [disabled]="!newRoute.model_pattern || !newRoute.backend_id">
                  <mat-icon>save</mat-icon> Create Route
                </button>
                <button mat-button (click)="showAddRoute = false">Cancel</button>
              </mat-card-actions>
            </mat-card>

            <table mat-table [dataSource]="routes" class="full-width section-table">
              <ng-container matColumnDef="pattern">
                <th mat-header-cell *matHeaderCellDef>Model Pattern</th>
                <td mat-cell *matCellDef="let r"><code>{{ r.model_pattern }}</code></td>
              </ng-container>
              <ng-container matColumnDef="backend">
                <th mat-header-cell *matHeaderCellDef>Backend</th>
                <td mat-cell *matCellDef="let r">{{ backendName(r.backend_id) }}</td>
              </ng-container>
              <ng-container matColumnDef="priority">
                <th mat-header-cell *matHeaderCellDef>Priority</th>
                <td mat-cell *matCellDef="let r">{{ r.priority }}</td>
              </ng-container>
              <ng-container matColumnDef="tags">
                <th mat-header-cell *matHeaderCellDef>Tags</th>
                <td mat-cell *matCellDef="let r">{{ r.tags || '-' }}</td>
              </ng-container>
              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef></th>
                <td mat-cell *matCellDef="let r">
                  <button mat-icon-button color="warn" (click)="deleteRoute(r)"><mat-icon>delete</mat-icon></button>
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="routeColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: routeColumns;"></tr>
            </table>
            <p *ngIf="routes.length === 0" class="empty-state">No model routes configured.</p>
          </div>
        </mat-tab>

        <!-- Playground Tab -->
        <mat-tab label="Playground">
          <div class="tab-content playground">
            <mat-card>
              <mat-card-header><mat-card-title>Inference Playground</mat-card-title></mat-card-header>
              <mat-card-content>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Prompt</mat-label>
                  <textarea matInput [(ngModel)]="testPrompt" rows="3"></textarea>
                </mat-form-field>
                <button mat-raised-button color="primary" (click)="testGenerate()" [disabled]="generating">
                  <mat-icon>{{ generating ? 'hourglass_empty' : 'send' }}</mat-icon>
                  {{ generating ? 'Generating...' : 'Generate' }}
                </button>
                <mat-card *ngIf="testResult" class="result-card">
                  <mat-card-content>
                    <div class="result-meta" *ngIf="testResult.backend">
                      <span><strong>Backend:</strong> {{ testResult.backend }}</span>
                      <span *ngIf="testResult.model"><strong>Model:</strong> {{ testResult.model }}</span>
                      <span *ngIf="testResult.latency_ms"><strong>Latency:</strong> {{ testResult.latency_ms }}ms</span>
                    </div>
                    <pre class="result-text">{{ testResult.response }}</pre>
                    <p *ngIf="testResult.error" class="result-error">Error: {{ testResult.error }}</p>
                  </mat-card-content>
                </mat-card>
              </mat-card-content>
            </mat-card>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    .header-left { display: flex; align-items: center; gap: 16px; }
    h1 { margin: 0; font-size: 24px; color: var(--text-primary); }
    .subtitle { margin: 4px 0 0; color: var(--text-secondary); font-size: 14px; }
    .summary-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .stat-card { padding: 20px; text-align: center; background: var(--bg-card); border: 1px solid var(--border); }
    .add-form-card { margin: 16px 0; background: var(--bg-card); border: 1px solid var(--accent); }
    .section-table { margin-top: 16px; background: transparent !important; }
    .full-width { width: 100%; }
    .tab-content { padding: 16px 0; }
    .tab-toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
    .toolbar-spacer { flex: 1; }
    .form-row { display: flex; gap: 16px; margin-bottom: 8px; }
    .form-row mat-form-field { flex: 1; }
    .status-online { color: #4caf50; }
    .status-offline { color: #f44336; }
    .primary-badge { color: #ffc107; font-size: 16px; width: 16px; height: 16px; vertical-align: middle; margin-left: 4px; }
    .playground mat-card { margin-top: 16px; background: var(--bg-card); }
    .result-card { margin-top: 16px; background: var(--bg-primary); }
    .result-meta { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; color: var(--text-secondary); font-size: 13px; }
    .result-text { white-space: pre-wrap; color: var(--text-primary); font-size: 13px; margin: 0; }
    .result-error { color: #f44336; margin-top: 8px; font-size: 13px; }
    .empty-state { text-align: center; padding: 40px; color: var(--text-secondary); }
    table th, table td { color: var(--text-primary) !important; }
    code { color: var(--accent); background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 3px; }
  `],
})
export class AiOrchestratorComponent implements OnInit {
  backends: AIBackend[] = [];
  routes: AIModelRoute[] = [];
  summary: AIFleetSummary | null = null;
  showAddBackend = false;
  showAddRoute = false;
  generating = false;
  testPrompt = 'Hello from TrueNorth Range';
  testResult: any = null;

  backendColumns = ['status', 'name', 'type', 'url', 'actions'];
  routeColumns = ['pattern', 'backend', 'priority', 'tags', 'actions'];

  newBackend = {
    name: '', backend_type: 'ollama', base_url: '', api_key: '',
    max_concurrent: 10, timeout_seconds: 120,
  };

  newRoute = { model_pattern: '', backend_id: '', priority: 0, tags: '' };

  constructor(private http: HttpClient, private snack: MatSnackBar) {}

  ngOnInit(): void {
    this.loadBackends();
    this.loadRoutes();
    this.loadSummary();
  }

  loadBackends(): void {
    this.http.get<AIBackend[]>('/api/ai-config/backends').subscribe({
      next: b => this.backends = b,
      error: () => this.snack.open('Failed to load backends', 'OK', { duration: 3000 }),
    });
  }

  loadRoutes(): void {
    this.http.get<AIModelRoute[]>('/api/ai-config/routes').subscribe({
      next: r => this.routes = r,
      error: () => this.snack.open('Failed to load routes', 'OK', { duration: 3000 }),
    });
  }

  loadSummary(): void {
    this.http.get<AIFleetSummary>('/api/ai-config/summary').subscribe({ next: s => this.summary = s, error: () => {} });
  }

  backendName(id: string): string {
    const b = this.backends.find(x => x.id === id);
    return b ? b.name : id.substring(0, 8) + '...';
  }

  createBackend(): void {
    this.http.post('/api/ai-config/backends', this.newBackend).subscribe({
      next: () => {
        this.showAddBackend = false;
        this.loadBackends();
        this.loadSummary();
        this.snack.open('Backend created', '', { duration: 2000, panelClass: 'snack-success' });
      },
      error: e => this.snack.open('Failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }

  setPrimary(b: AIBackend): void {
    this.http.post(`/api/ai-config/backends/${b.id}/set-primary`, {}).subscribe({
      next: () => this.loadBackends(),
      error: () => this.snack.open('Failed to set primary', 'OK', { duration: 3000 }),
    });
  }

  deleteBackend(b: AIBackend): void {
    if (confirm(`Delete backend "${b.name}"?`)) {
      this.http.delete(`/api/ai-config/backends/${b.id}`).subscribe({
        next: () => { this.loadBackends(); this.loadSummary(); },
        error: () => this.snack.open('Failed to delete', 'OK', { duration: 3000 }),
      });
    }
  }

  createRoute(): void {
    this.http.post('/api/ai-config/routes', this.newRoute).subscribe({
      next: () => {
        this.showAddRoute = false;
        this.newRoute = { model_pattern: '', backend_id: '', priority: 0, tags: '' };
        this.loadRoutes();
        this.loadSummary();
        this.snack.open('Route created', '', { duration: 2000, panelClass: 'snack-success' });
      },
      error: e => this.snack.open('Failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 }),
    });
  }

  deleteRoute(r: AIModelRoute): void {
    this.http.delete(`/api/ai-config/routes/${r.id}`).subscribe({
      next: () => { this.loadRoutes(); this.loadSummary(); },
      error: () => this.snack.open('Failed to delete route', 'OK', { duration: 3000 }),
    });
  }

  testGenerate(): void {
    this.generating = true;
    this.testResult = null;
    this.http.post('/api/ai-config/test-generate', null, {
      params: { prompt: this.testPrompt }
    }).subscribe({
      next: r => { this.testResult = r; this.generating = false; },
      error: e => {
        this.snack.open('Generation failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 });
        this.generating = false;
      },
    });
  }
}
