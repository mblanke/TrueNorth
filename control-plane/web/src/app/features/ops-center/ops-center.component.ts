import { Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatInputModule } from '@angular/material/input';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { MatListModule } from '@angular/material/list';
import { MatBadgeModule } from '@angular/material/badge';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { EnterStaggerDirective } from '../../shared/motion';

interface Annotation {
  id: string;
  user_display_name: string;
  content: string;
  annotation_type: string;
  severity: string;
  tags: string[];
  created_at: string;
}

interface SharedCmd {
  id: string;
  user_display_name: string;
  command: string;
  description: string;
  host_tag: string;
  shared_at: string;
}

interface OpsStats {
  exercise_id: string;
  active_analysts: number;
  annotations_count: number;
  shared_commands_count: number;
  objectives_completed: number;
  objectives_total: number;
  elapsed_seconds: number;
}

@Component({
  selector: 'tn-ops-center',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatTabsModule, MatInputModule, MatFormFieldModule, MatSelectModule,
    MatChipsModule, MatListModule, MatBadgeModule, EnterStaggerDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">radar</mat-icon>
          <div>
            <h1>Ops Center</h1>
            <p class="subtitle">Live exercise collaboration — Exercise {{ exerciseId }}</p>
          </div>
        </div>
        <div class="header-actions">
          <button mat-stroked-button (click)="refreshAll()">
            <mat-icon>refresh</mat-icon> Refresh
          </button>
        </div>
      </div>

      <!-- Stats Bar -->
      <div class="stats-row" tnEnterStagger>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">group</mat-icon>
            <div class="stat-value">{{ stats()?.active_analysts || 0 }}</div>
            <div class="stat-label">Active Analysts</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">note_add</mat-icon>
            <div class="stat-value">{{ stats()?.annotations_count || 0 }}</div>
            <div class="stat-label">Annotations</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">terminal</mat-icon>
            <div class="stat-value">{{ stats()?.shared_commands_count || 0 }}</div>
            <div class="stat-label">Shared Commands</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">flag</mat-icon>
            <div class="stat-value">{{ stats()?.objectives_completed || 0 }}/{{ stats()?.objectives_total || 0 }}</div>
            <div class="stat-label">Objectives</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon color="primary">timer</mat-icon>
            <div class="stat-value">{{ formatElapsed(stats()?.elapsed_seconds || 0) }}</div>
            <div class="stat-label">Elapsed</div>
          </mat-card-content>
        </mat-card>
      </div>

      <mat-tab-group class="mt-2">
        <!-- Annotations Tab -->
        <mat-tab label="Annotations">
          <div class="tab-content">
            <mat-card class="input-card">
              <mat-card-content>
                <div class="input-row">
                  <mat-form-field class="flex-grow">
                    <mat-label>Add annotation</mat-label>
                    <textarea matInput [(ngModel)]="newAnnotation" rows="2"></textarea>
                  </mat-form-field>
                  <mat-form-field>
                    <mat-label>Type</mat-label>
                    <mat-select [(ngModel)]="annotationType">
                      <mat-option value="observation">Observation</mat-option>
                      <mat-option value="finding">Finding</mat-option>
                      <mat-option value="recommendation">Recommendation</mat-option>
                      <mat-option value="ioc">IOC</mat-option>
                    </mat-select>
                  </mat-form-field>
                  <mat-form-field>
                    <mat-label>Severity</mat-label>
                    <mat-select [(ngModel)]="annotationSeverity">
                      <mat-option value="info">Info</mat-option>
                      <mat-option value="low">Low</mat-option>
                      <mat-option value="medium">Medium</mat-option>
                      <mat-option value="high">High</mat-option>
                      <mat-option value="critical">Critical</mat-option>
                    </mat-select>
                  </mat-form-field>
                  <button mat-flat-button color="primary" (click)="submitAnnotation()" [disabled]="!newAnnotation.trim()">
                    <mat-icon>send</mat-icon>
                  </button>
                </div>
              </mat-card-content>
            </mat-card>

            <mat-list>
              @for (a of annotations(); track a.id) {
                <mat-list-item class="annotation-item">
                  <mat-icon matListItemIcon [class]="'severity-' + a.severity">
                    {{ a.annotation_type === 'ioc' ? 'bug_report' : a.annotation_type === 'finding' ? 'search' : 'comment' }}
                  </mat-icon>
                  <div matListItemTitle>
                    <strong>{{ a.user_display_name }}</strong>
                    <span class="type-badge" [attr.data-type]="a.annotation_type">{{ a.annotation_type }}</span>
                    <span class="severity-badge" [attr.data-severity]="a.severity">{{ a.severity }}</span>
                  </div>
                  <div matListItemLine>{{ a.content }}</div>
                  <div matListItemMeta>{{ a.created_at | date:'shortTime' }}</div>
                </mat-list-item>
              } @empty {
                <mat-card><mat-card-content>No annotations yet. Add observations as you investigate.</mat-card-content></mat-card>
              }
            </mat-list>
          </div>
        </mat-tab>

        <!-- Shared Commands Tab -->
        <mat-tab label="Shared Commands">
          <div class="tab-content">
            <mat-card class="input-card">
              <mat-card-content>
                <div class="input-row">
                  <mat-form-field class="flex-grow">
                    <mat-label>Share a command</mat-label>
                    <input matInput [(ngModel)]="newCommand" placeholder="e.g. Get-WinEvent -LogName Security | ...">
                  </mat-form-field>
                  <mat-form-field>
                    <mat-label>Host</mat-label>
                    <input matInput [(ngModel)]="commandHost" placeholder="e.g. DC01">
                  </mat-form-field>
                  <button mat-flat-button color="primary" (click)="submitCommand()" [disabled]="!newCommand.trim()">
                    <mat-icon>send</mat-icon>
                  </button>
                </div>
              </mat-card-content>
            </mat-card>

            @for (cmd of sharedCommands(); track cmd.id) {
              <mat-card class="command-card">
                <mat-card-header>
                  <mat-icon mat-card-avatar>terminal</mat-icon>
                  <mat-card-title>{{ cmd.user_display_name }}</mat-card-title>
                  <mat-card-subtitle>
                    {{ cmd.shared_at | date:'shortTime' }}
                    @if (cmd.host_tag) { — {{ cmd.host_tag }} }
                  </mat-card-subtitle>
                </mat-card-header>
                <mat-card-content>
                  <pre class="command-text">{{ cmd.command }}</pre>
                  @if (cmd.description) {
                    <p class="command-desc">{{ cmd.description }}</p>
                  }
                </mat-card-content>
              </mat-card>
            } @empty {
              <mat-card><mat-card-content>No shared commands yet.</mat-card-content></mat-card>
            }
          </div>
        </mat-tab>

        <!-- Instructor Panel Tab -->
        <mat-tab label="Instructor">
          <div class="tab-content">
            <mat-card>
              <mat-card-header>
                <mat-card-title>Send Live Inject</mat-card-title>
                <mat-card-subtitle>Push a simulated event to all analysts</mat-card-subtitle>
              </mat-card-header>
              <mat-card-content>
                <div class="inject-form">
                  <mat-form-field class="full-width">
                    <mat-label>Inject Type</mat-label>
                    <mat-select [(ngModel)]="injectType">
                      <mat-option value="simulated_execution">Simulated Execution</mat-option>
                      <mat-option value="dns_spike">DNS Spike</mat-option>
                      <mat-option value="http_burst">HTTP Burst</mat-option>
                      <mat-option value="email_phish">Email Phish</mat-option>
                      <mat-option value="custom">Custom</mat-option>
                    </mat-select>
                  </mat-form-field>
                  <mat-form-field class="full-width">
                    <mat-label>Description</mat-label>
                    <textarea matInput [(ngModel)]="injectDescription" rows="2"></textarea>
                  </mat-form-field>
                  <button mat-flat-button color="warn" (click)="sendInject()">
                    <mat-icon>bolt</mat-icon> Send Inject
                  </button>
                </div>
              </mat-card-content>
            </mat-card>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 16px; }
    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stats-row mat-card { flex: 1; min-width: 120px; text-align: center; }
    .tab-content { padding: 16px 0; }
    .mt-2 { margin-top: 16px; }
    .input-card { margin-bottom: 16px; }
    .input-row { display: flex; gap: 12px; align-items: flex-start; }
    .flex-grow { flex: 1; }
    .full-width { width: 100%; }
    .command-card { margin-bottom: 8px; }
    .command-text { background: var(--mat-sys-surface-container); padding: 8px 12px; border-radius: 4px; overflow-x: auto; font-family: monospace; font-size: 13px; }
    .command-desc { color: var(--text-secondary); font-size: 13px; margin-top: 4px; }
    .type-badge { font-size: 11px; padding: 1px 6px; border-radius: 4px; margin-left: 8px; background: var(--mat-sys-primary-container); }
    .severity-badge { font-size: 11px; padding: 1px 6px; border-radius: 4px; margin-left: 4px; }
    .severity-badge[data-severity="critical"] { background: #dc2626; color: white; }
    .severity-badge[data-severity="high"] { background: #ea580c; color: white; }
    .severity-badge[data-severity="medium"] { background: #d97706; color: white; }
    .severity-badge[data-severity="low"] { background: #2563eb; color: white; }
    .severity-badge[data-severity="info"] { background: #6b7280; color: white; }
    .inject-form { display: flex; flex-direction: column; gap: 12px; }
    .annotation-item { margin-bottom: 4px; }
  `],
})
export class OpsCenterComponent implements OnInit, OnDestroy {
  exerciseId = '';
  annotations = signal<Annotation[]>([]);
  sharedCommands = signal<SharedCmd[]>([]);
  stats = signal<OpsStats | null>(null);

  newAnnotation = '';
  annotationType = 'observation';
  annotationSeverity = 'info';
  newCommand = '';
  commandHost = '';
  injectType = 'simulated_execution';
  injectDescription = '';

  private refreshInterval: ReturnType<typeof setInterval> | null = null;

  constructor(
    private route: ActivatedRoute,
    private api: ApiService,
    private notify: NotificationService,
  ) {}

  ngOnInit() {
    this.exerciseId = this.route.snapshot.paramMap.get('exerciseId') || '';
    if (this.exerciseId) {
      this.refreshAll();
      this.refreshInterval = setInterval(() => this.loadStats(), 15000);
    }
  }

  ngOnDestroy() {
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
    }
  }

  refreshAll() {
    this.loadAnnotations();
    this.loadCommands();
    this.loadStats();
  }

  loadAnnotations() {
    this.api.get<Annotation[]>(`/ops/exercises/${this.exerciseId}/annotations`).subscribe({
      next: a => this.annotations.set(a),
      error: () => {},
    });
  }

  loadCommands() {
    this.api.get<SharedCmd[]>(`/ops/exercises/${this.exerciseId}/commands`).subscribe({
      next: c => this.sharedCommands.set(c),
      error: () => {},
    });
  }

  loadStats() {
    this.api.get<OpsStats>(`/ops/exercises/${this.exerciseId}/stats`).subscribe({
      next: s => this.stats.set(s),
      error: () => {},
    });
  }

  submitAnnotation() {
    if (!this.newAnnotation.trim()) return;
    this.api.post(`/ops/exercises/${this.exerciseId}/annotations`, {
      content: this.newAnnotation,
      annotation_type: this.annotationType,
      severity: this.annotationSeverity,
      tags: [],
    }).subscribe({
      next: () => {
        this.newAnnotation = '';
        this.loadAnnotations();
        this.loadStats();
      },
      error: () => this.notify.error('Failed to create annotation'),
    });
  }

  submitCommand() {
    if (!this.newCommand.trim()) return;
    this.api.post(`/ops/exercises/${this.exerciseId}/commands`, {
      command: this.newCommand,
      host_tag: this.commandHost,
    }).subscribe({
      next: () => {
        this.newCommand = '';
        this.commandHost = '';
        this.loadCommands();
        this.loadStats();
      },
      error: () => this.notify.error('Failed to share command'),
    });
  }

  sendInject() {
    this.api.post(`/ops/exercises/${this.exerciseId}/inject`, {
      inject_type: this.injectType,
      description: this.injectDescription,
    }).subscribe({
      next: () => {
        this.notify.success('Inject sent');
        this.injectDescription = '';
      },
      error: () => this.notify.error('Failed to send inject'),
    });
  }

  formatElapsed(seconds: number): string {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
}
