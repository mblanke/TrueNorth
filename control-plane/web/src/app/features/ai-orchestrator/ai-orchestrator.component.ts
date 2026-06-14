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
import { MatBadgeModule } from '@angular/material/badge';
import { HttpClient } from '@angular/common/http';
import { MatSnackBar } from '@angular/material/snack-bar';
import { EnterStaggerDirective } from '../../shared/motion';

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

interface _AIFleetNode {
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

interface DiscoveredModel {
  name: string;
  size_bytes: number;
  family: string;
  parameter_size: string;
  quantization: string;
}

interface DiscoverNodeResult {
  node_name: string;
  url: string;
  online: boolean;
  version: string | null;
  gpu_model: string | null;
  gpu_vram_gb: number | null;
  model_count: number;
  models: DiscoveredModel[];
  running: string[];
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
  selector: 'tn-ai-orchestrator',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatTabsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatTableModule, MatChipsModule, MatFormFieldModule,
    MatInputModule, MatSelectModule, MatTooltipModule, MatProgressBarModule,
    MatBadgeModule, EnterStaggerDirective,
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
      @if (summary) {
        <div class="summary-row" tnEnterStagger>
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
      }

      <mat-tab-group>
        <!-- Backends Tab -->
        <mat-tab label="Backends">
          @if (showAddBackend || editingBackendId) {
            <mat-card class="add-form-card">
              <mat-card-header><mat-card-title>{{ editingBackendId ? 'Edit AI Backend' : 'New AI Backend' }}</mat-card-title></mat-card-header>
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
                <button mat-raised-button color="primary"
                  (click)="editingBackendId ? updateBackend() : createBackend()"
                  [disabled]="backendSaving">
                  {{ editingBackendId ? (backendSaving ? 'Saving...' : 'Save Changes') : 'Save' }}
                </button>
                <button mat-button (click)="editingBackendId ? cancelBackendEdit() : (showAddBackend = false)">Cancel</button>
              </mat-card-actions>
            </mat-card>
          }

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
                @if (b.is_primary) { <mat-icon class="primary-badge" matTooltip="Primary">star</mat-icon> }
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
                <button mat-icon-button matTooltip="Edit" (click)="startEditBackend(b)">
                  <mat-icon>edit</mat-icon>
                </button>
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

        <!-- Fleet Nodes Tab -->
        <mat-tab>
          <ng-template mat-tab-label>
            Fleet Nodes
            @if (discoveredNodes.length > 0) {
              <span class="tab-badge">{{ discoveredNodes.length }}</span>
            }
          </ng-template>
          <div class="tab-content">
            <div class="tab-toolbar">
              <span class="toolbar-title">R7725 GPU Fleet &mdash; 2&times; NVIDIA H200 NVL</span>
              <span class="toolbar-spacer"></span>
              <button mat-raised-button color="accent" (click)="scanFleet()" [disabled]="scanning">
                <mat-icon>{{ scanning ? 'hourglass_empty' : 'radar' }}</mat-icon>
                {{ scanning ? 'Scanning...' : 'Scan Fleet' }}
              </button>
            </div>

            @if (scanning) {
              <mat-progress-bar mode="indeterminate" class="scan-progress"></mat-progress-bar>
            }

            @if (discoveredNodes.length > 0) {
              <div class="fleet-grid">
                @for (node of discoveredNodes; track node.node_name) {
                  <mat-card class="node-card" [class.node-offline]="!node.online">
                    <mat-card-header>
                      <mat-icon mat-card-avatar [class]="node.online ? 'status-online' : 'status-offline'">
                        {{ node.online ? 'dns' : 'cloud_off' }}
                      </mat-icon>
                      <mat-card-title>{{ node.node_name }}</mat-card-title>
                      <mat-card-subtitle>{{ node.url }}</mat-card-subtitle>
                    </mat-card-header>
                    <mat-card-content>
                      <div class="node-meta">
                        <div class="meta-row">
                          <mat-icon>developer_board</mat-icon>
                          <span>{{ node.gpu_model || 'Unknown GPU' }}</span>
                        </div>
                        <div class="meta-row">
                          <mat-icon>memory</mat-icon>
                          <span>{{ node.gpu_vram_gb || 0 }} GB VRAM</span>
                        </div>
                        @if (node.version) {
                          <div class="meta-row">
                            <mat-icon>info_outline</mat-icon>
                            <span>Ollama v{{ node.version }}</span>
                          </div>
                        }
                        <div class="meta-row">
                          <mat-icon>{{ node.online ? 'check_circle' : 'error' }}</mat-icon>
                          <span [class]="node.online ? 'text-green' : 'text-red'">
                            {{ node.online ? 'Online' : 'Offline / Unreachable' }}
                          </span>
                        </div>
                      </div>

                      @if (node.models.length > 0) {
                        <div class="models-section">
                          <div class="models-header">
                            <mat-icon>smart_toy</mat-icon>
                            <strong>{{ node.model_count }} Models Available</strong>
                          </div>
                          <div class="model-list">
                            @for (m of node.models; track m.name) {
                              <div class="model-chip" [class.model-running]="node.running.includes(m.name)">
                                <span class="model-name">{{ m.name }}</span>
                                <span class="model-meta">{{ m.parameter_size }} &bull; {{ m.quantization }} &bull; {{ m.family }}</span>
                                <span class="model-size">{{ formatBytes(m.size_bytes) }}</span>
                                @if (node.running.includes(m.name)) {
                                  <mat-icon class="running-icon" matTooltip="Currently loaded">play_circle</mat-icon>
                                }
                              </div>
                            }
                          </div>
                        </div>
                      } @else if (node.online) {
                        <p class="empty-models">No models found on this node.</p>
                      }
                    </mat-card-content>
                  </mat-card>
                }
              </div>
            } @else if (!scanning) {
              <div class="empty-fleet">
                <mat-icon>radar</mat-icon>
                <p>No fleet scan results yet. Click <strong>Scan Fleet</strong> to probe the configured AI engine and list its models.</p>
                <p class="fleet-hint">Scanning 192.168.1.50 (wile) &amp; 192.168.1.51 (roadrunner)</p>
              </div>
            }
          </div>
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

            @if (showAddRoute || editingRouteId) {
              <mat-card class="add-form-card">
                <mat-card-header><mat-card-title>{{ editingRouteId ? 'Edit Model Route' : 'New Model Route' }}</mat-card-title></mat-card-header>
                <mat-card-content>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Model Pattern</mat-label>
                      <input matInput [(ngModel)]="newRoute.model_pattern" placeholder="llama3*">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Backend</mat-label>
                      <mat-select [(ngModel)]="newRoute.backend_id" panelClass="tn-select-panel">
                        @for (b of backends; track b.id) {
                          <mat-option [value]="b.id">{{ b.name }} ({{ b.backend_type }})</mat-option>
                        }
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
                  <button mat-raised-button color="primary"
                    (click)="editingRouteId ? updateRoute() : createRoute()"
                    [disabled]="editingRouteId ? routeSaving : (!newRoute.model_pattern || !newRoute.backend_id)">
                    <mat-icon>save</mat-icon>
                    {{ editingRouteId ? (routeSaving ? 'Saving...' : 'Save Changes') : 'Create Route' }}
                  </button>
                  <button mat-button (click)="editingRouteId ? cancelRouteEdit() : (showAddRoute = false)">Cancel</button>
                </mat-card-actions>
              </mat-card>
            }

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
                  <button mat-icon-button matTooltip="Edit" (click)="startEditRoute(r)"><mat-icon>edit</mat-icon></button>
                  <button mat-icon-button color="warn" matTooltip="Delete" (click)="deleteRoute(r)"><mat-icon>delete</mat-icon></button>
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="routeColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: routeColumns;"></tr>
            </table>
            @if (routes.length === 0) {
              <p class="empty-state">No model routes configured.</p>
            }
          </div>
        </mat-tab>

        <!-- Playground Tab -->
        <mat-tab label="Playground">
          <div class="tab-content playground">
            <mat-card>
              <mat-card-header><mat-card-title>Inference Playground</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline" class="full-width">
                    <mat-label>Model</mat-label>
                    <mat-select [(ngModel)]="selectedModel" panelClass="tn-select-panel">
                      @for (m of availableModels; track m.name) {
                        <mat-option [value]="m.name">{{ m.name }} <span class="model-node-hint">({{ m.node }})</span></mat-option>
                      }
                      @if (availableModels.length === 0) {
                        <mat-option value="llama3.1:latest">llama3.1:latest (default)</mat-option>
                      }
                    </mat-select>
                  </mat-form-field>
                </div>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Prompt</mat-label>
                  <textarea matInput [(ngModel)]="testPrompt" rows="3"></textarea>
                </mat-form-field>
                <button mat-raised-button color="primary" (click)="testGenerate()" [disabled]="generating">
                  <mat-icon>{{ generating ? 'hourglass_empty' : 'send' }}</mat-icon>
                  {{ generating ? 'Generating...' : 'Generate' }}
                </button>
                @if (testResult) {
                  <mat-card class="result-card">
                    <mat-card-content>
                      @if (testResult.backend) {
                        <div class="result-meta">
                          <span><strong>Backend:</strong> {{ testResult.backend }}</span>
                          @if (testResult.model) { <span><strong>Model:</strong> {{ testResult.model }}</span> }
                          @if (testResult.node) { <span><strong>Node:</strong> {{ testResult.node }}</span> }
                          @if (testResult.latency_ms) { <span><strong>Latency:</strong> {{ testResult.latency_ms }}ms</span> }
                        </div>
                      }
                      <pre class="result-text">{{ testResult.response }}</pre>
                      @if (testResult.error) { <p class="result-error">Error: {{ testResult.error }}</p> }
                    </mat-card-content>
                  </mat-card>
                }
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
    .toolbar-title { font-size: 16px; font-weight: 500; color: var(--text-primary); }
    .toolbar-spacer { flex: 1; }
    .form-row { display: flex; gap: 16px; margin-bottom: 8px; }
    .form-row mat-form-field { flex: 1; }
    .status-online { color: #4caf50; }
    .status-offline { color: #f44336; }
    .text-green { color: #4caf50; }
    .text-red { color: #f44336; }
    .primary-badge { color: #ffc107; font-size: 16px; width: 16px; height: 16px; vertical-align: middle; margin-left: 4px; }
    .playground mat-card { margin-top: 16px; background: var(--bg-card); }
    .result-card { margin-top: 16px; background: var(--bg-primary); }
    .result-meta { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; color: var(--text-secondary); font-size: 13px; }
    .result-text { white-space: pre-wrap; color: var(--text-primary); font-size: 13px; margin: 0; }
    .result-error { color: #f44336; margin-top: 8px; font-size: 13px; }
    .empty-state { text-align: center; padding: 40px; color: var(--text-secondary); }
    table th, table td { color: var(--text-primary) !important; }
    code { color: var(--accent); background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 3px; }
    .tab-badge { background: var(--accent); color: #fff; border-radius: 10px; padding: 1px 8px; font-size: 11px; margin-left: 8px; }

    /* Fleet Nodes */
    .scan-progress { margin-bottom: 16px; }
    .fleet-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: 20px; }
    .node-card { background: var(--bg-card); border: 1px solid var(--border); transition: border-color 0.2s; }
    .node-card:hover { border-color: var(--accent); }
    .node-offline { opacity: 0.65; border-color: #f44336; }
    .node-meta { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
    .meta-row { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 13px; }
    .meta-row mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-secondary); }
    .models-section { margin-top: 16px; }
    .models-header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; color: var(--text-primary); font-size: 14px; }
    .model-list { display: flex; flex-direction: column; gap: 6px; max-height: 400px; overflow-y: auto; }
    .model-chip { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 6px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); font-size: 13px; }
    .model-chip:hover { background: rgba(255,255,255,0.08); }
    .model-running { border-color: #4caf50; background: rgba(76,175,80,0.08); }
    .model-name { font-weight: 500; color: var(--text-primary); min-width: 200px; }
    .model-meta { color: var(--text-secondary); font-size: 12px; flex: 1; }
    .model-size { color: var(--text-secondary); font-size: 12px; white-space: nowrap; }
    .running-icon { color: #4caf50; font-size: 16px; width: 16px; height: 16px; }
    .empty-models { color: var(--text-secondary); font-size: 13px; padding: 8px 0; }
    .empty-fleet { text-align: center; padding: 60px 20px; color: var(--text-secondary); }
    .empty-fleet mat-icon { font-size: 64px; width: 64px; height: 64px; margin-bottom: 16px; opacity: 0.4; }
    .empty-fleet p { margin: 8px 0; }
    .fleet-hint { font-size: 12px; opacity: 0.6; }
    .model-node-hint { font-size: 11px; color: var(--text-secondary); opacity: 0.7; }
  `],
})
export class AiOrchestratorComponent implements OnInit {
  backends: AIBackend[] = [];
  routes: AIModelRoute[] = [];
  summary: AIFleetSummary | null = null;
  showAddBackend = false;
  showAddRoute = false;
  generating = false;
  scanning = false;
  editingBackendId: string | null = null;
  backendSaving = false;
  editingRouteId: string | null = null;
  routeSaving = false;
  testPrompt = 'Hello from TrueNorth Range';
  selectedModel = 'llama3.1:latest';
  availableModels: { name: string; node: string }[] = [];
  testResult: any = null;
  discoveredNodes: DiscoverNodeResult[] = [];

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
    this.loadModels();
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

  loadModels(): void {
    this.http.get<{ models: { name: string; node: string }[]; count: number }>('/api/ai-config/models').subscribe({
      next: res => this.availableModels = res.models,
      error: () => {},
    });
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

  startEditBackend(b: AIBackend): void {
    this.showAddBackend = false;
    this.editingBackendId = b.id;
    this.newBackend = {
      name: b.name, backend_type: b.backend_type, base_url: b.base_url, api_key: '',
      max_concurrent: b.max_concurrent, timeout_seconds: b.timeout_seconds,
    };
  }

  updateBackend(): void {
    if (!this.editingBackendId) return;
    this.backendSaving = true;
    this.http.patch(`/api/ai-config/backends/${this.editingBackendId}`, this.newBackend).subscribe({
      next: () => {
        this.cancelBackendEdit();
        this.loadBackends();
        this.loadSummary();
        this.snack.open('Backend updated', 'OK', { duration: 3000 });
      },
      error: e => {
        this.backendSaving = false;
        this.snack.open('Failed to update: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 });
      },
    });
  }

  cancelBackendEdit(): void {
    this.editingBackendId = null;
    this.backendSaving = false;
    this.newBackend = {
      name: '', backend_type: 'ollama', base_url: '', api_key: '',
      max_concurrent: 10, timeout_seconds: 120,
    };
  }

  scanFleet(): void {
    const primary = this.backends.find(b => b.is_primary);
    if (!primary) {
      this.snack.open('No primary backend configured. Add an Ollama backend first.', 'OK', { duration: 4000 });
      return;
    }
    this.scanning = true;
    this.http.post<{ scanned: number; results: DiscoverNodeResult[] }>(
      `/api/ai-config/backends/${primary.id}/discover`, {}
    ).subscribe({
      next: res => {
        this.discoveredNodes = res.results;
        this.scanning = false;
        this.loadSummary();
        this.loadModels();
        const online = res.results.filter(n => n.online).length;
        const total = res.scanned;
        const models = res.results.reduce((a, n) => a + n.model_count, 0);
        this.snack.open(
          `Scan complete: ${online}/${total} nodes online, ${models} models found`,
          '', { duration: 4000, panelClass: 'snack-success' }
        );
      },
      error: e => {
        this.scanning = false;
        this.snack.open('Scan failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 });
      },
    });
  }

  formatBytes(bytes: number): string {
    if (bytes === 0) return '0 B';
    const gb = bytes / (1024 * 1024 * 1024);
    if (gb >= 1) return gb.toFixed(1) + ' GB';
    const mb = bytes / (1024 * 1024);
    return mb.toFixed(0) + ' MB';
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

  startEditRoute(r: AIModelRoute): void {
    this.showAddRoute = false;
    this.editingRouteId = r.id;
    this.newRoute = {
      model_pattern: r.model_pattern, backend_id: r.backend_id,
      priority: r.priority, tags: r.tags || '',
    };
  }

  updateRoute(): void {
    if (!this.editingRouteId) return;
    this.routeSaving = true;
    this.http.patch(`/api/ai-config/routes/${this.editingRouteId}`, this.newRoute).subscribe({
      next: () => {
        this.cancelRouteEdit();
        this.loadRoutes();
        this.loadSummary();
        this.snack.open('Route updated', 'OK', { duration: 3000 });
      },
      error: e => {
        this.routeSaving = false;
        this.snack.open('Failed to update: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 });
      },
    });
  }

  cancelRouteEdit(): void {
    this.editingRouteId = null;
    this.routeSaving = false;
    this.newRoute = { model_pattern: '', backend_id: '', priority: 0, tags: '' };
  }

  testGenerate(): void {
    this.generating = true;
    this.testResult = null;
    this.http.post('/api/ai-config/test-generate', null, {
      params: { prompt: this.testPrompt, model: this.selectedModel }
    }).subscribe({
      next: r => { this.testResult = r; this.generating = false; },
      error: e => {
        this.snack.open('Generation failed: ' + (e.error?.detail || e.message), 'OK', { duration: 4000 });
        this.generating = false;
      },
    });
  }
}
