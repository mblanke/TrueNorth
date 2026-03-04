import { Component, OnInit, OnDestroy, signal, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatDialogModule } from '@angular/material/dialog';
import { ApiService } from '@core/services/api.service';
import { HttpClient } from '@angular/common/http';
import { Range, Exercise, HealthResponse } from '@core/models';

interface ClusterNode {
  node: string;
  status: string;
  ip: string;
  cpu_pct: number;
  maxcpu: number;
  mem_used_gb: number;
  mem_total_gb: number;
  uptime_h: number;
  vms: any[];
  networks: any[];
}

interface CapacityInfo {
  vcpu_available: number;
  vcpu_committed: number;
  vcpu_total: number;
  ram_mb_available: number;
  ram_mb_committed: number;
  ram_mb_total: number;
  disk_gb_available: number;
  disk_gb_committed: number;
  disk_gb_total: number;
  overlapping_events: number;
  message: string;
  fits?: boolean;
  detail?: string;
}

interface ScheduledEvent {
  id: string;
  name: string;
  description: string | null;
  state: string;
  start_time: string;
  end_time: string;
  vm_count: number;
  vcpu_total: number;
  ram_mb_total: number;
  disk_gb_total: number;
}

/* Deployment profile pre-sets */
interface DeploymentProfile {
  name: string;
  icon: string;
  vm_count: number;
  vcpu_per_vm: number;
  ram_mb_per_vm: number;
  disk_gb_per_vm: number;
}

@Component({
  selector: 'tn-dashboard',
  standalone: true,
  imports: [
    CommonModule, RouterModule, FormsModule, MatCardModule, MatIconModule,
    MatButtonModule, MatChipsModule, MatProgressBarModule, MatTooltipModule,
    MatDividerModule, MatFormFieldModule, MatInputModule, MatSnackBarModule,
    MatDialogModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">dashboard</mat-icon>
          <div>
            <h1>Dashboard</h1>
            <p class="subtitle">System overview and quick actions</p>
          </div>
        </div>
      </div>

      <!-- ──── TOP STATS ROW ────────────────────────────────────────────────────────────────────── -->
      <div class="stats-row">
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon>dns</mat-icon>
            <div class="stat-value">{{ rangeCount() }}</div>
            <div class="stat-label">Active Ranges</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon>fitness_center</mat-icon>
            <div class="stat-value">{{ exerciseCount() }}</div>
            <div class="stat-label">Exercises</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card" [matTooltip]="healthTooltip()">
          <mat-card-content>
            <mat-icon [style.color]="health()?.status === 'ok' ? 'var(--success)' : 'var(--alert)'">
              {{ health()?.status === 'ok' ? 'check_circle' : 'error' }}
            </mat-icon>
            <div class="stat-value">{{ health()?.status || '...' }}</div>
            <div class="stat-label">System Health</div>
          </mat-card-content>
        </mat-card>
        <mat-card class="stat-card">
          <mat-card-content>
            <mat-icon>storage</mat-icon>
            <div class="stat-value">{{ clusterNodes().length }}</div>
            <div class="stat-label">Proxmox Nodes</div>
          </mat-card-content>
        </mat-card>
      </div>

      <!-- ──── CLUSTER CAPACITY (SimSpace-style) ────────────────────────────── -->
      <h2 class="section-heading"><mat-icon>memory</mat-icon> Cluster Capacity</h2>
      <div class="cluster-panel">
        <!-- Node cards -->
        <div class="node-cards">
          @for (node of clusterNodes(); track node.node) {
            <mat-card class="node-card" [class.offline]="node.status !== 'online'">
              <div class="node-header">
                <mat-icon [style.color]="node.status === 'online' ? 'var(--success)' : 'var(--alert)'">
                  {{ node.status === 'online' ? 'check_circle' : 'error' }}
                </mat-icon>
                <span class="node-name">{{ node.node }}</span>
                <span class="node-ip">{{ node.ip }}</span>
              </div>
              <div class="gauge-row">
                <div class="gauge">
                  <div class="gauge-label">CPU</div>
                  <div class="gauge-bar-bg">
                    <div class="gauge-bar-fill" [style.width.%]="node.cpu_pct"
                         [class.warn]="node.cpu_pct > 70" [class.crit]="node.cpu_pct > 90">
                    </div>
                  </div>
                  <div class="gauge-val">{{ node.cpu_pct }}% of {{ node.maxcpu }} cores</div>
                </div>
                <div class="gauge">
                  <div class="gauge-label">RAM</div>
                  <div class="gauge-bar-bg">
                    <div class="gauge-bar-fill"
                         [style.width.%]="node.mem_total_gb ? (node.mem_used_gb / node.mem_total_gb * 100) : 0"
                         [class.warn]="node.mem_used_gb / node.mem_total_gb > 0.7"
                         [class.crit]="node.mem_used_gb / node.mem_total_gb > 0.9">
                    </div>
                  </div>
                  <div class="gauge-val">{{ node.mem_used_gb }}GB / {{ node.mem_total_gb }}GB</div>
                </div>
              </div>
              <div class="node-footer">
                <span><mat-icon class="sm-icon">computer</mat-icon> {{ node.vms.length }} VMs</span>
                <span><mat-icon class="sm-icon">lan</mat-icon> {{ node.networks.length }} bridges</span>
                <span><mat-icon class="sm-icon">schedule</mat-icon> {{ node.uptime_h | number:'1.0-0' }}h up</span>
              </div>
            </mat-card>
          }
          @if (clusterNodes().length === 0 && !clusterLoading()) {
            <mat-card class="node-card empty-card">
              <mat-icon class="empty-big">cloud_off</mat-icon>
              <p>No cluster data – API offline or Proxmox unreachable</p>
              <button mat-stroked-button (click)="refreshCluster()">
                <mat-icon>refresh</mat-icon> Retry
              </button>
            </mat-card>
          }
          @if (clusterLoading()) {
            <mat-card class="node-card empty-card">
              <mat-icon class="empty-big spin">sync</mat-icon>
              <p>Discovering Proxmox cluster...</p>
            </mat-card>
          }
        </div>

        <!-- Aggregate capacity bars -->
        @if (capacity()) {
          <div class="capacity-aggregate">
            <div class="cap-bar-group">
              <div class="cap-label">
                <mat-icon>developer_board</mat-icon> vCPU
                <span class="cap-numbers">{{ capacity()!.vcpu_committed }} / {{ capacity()!.vcpu_total }} committed</span>
              </div>
              <div class="cap-bar-bg">
                <div class="cap-bar-committed" [style.width.%]="capacity()!.vcpu_total ? (capacity()!.vcpu_committed / capacity()!.vcpu_total * 100) : 0"></div>
              </div>
              <div class="cap-avail">{{ capacity()!.vcpu_available }} vCPU available</div>
            </div>
            <div class="cap-bar-group">
              <div class="cap-label">
                <mat-icon>memory</mat-icon> RAM
                <span class="cap-numbers">{{ (capacity()!.ram_mb_committed / 1024) | number:'1.0-0' }}GB / {{ (capacity()!.ram_mb_total / 1024) | number:'1.0-0' }}GB</span>
              </div>
              <div class="cap-bar-bg">
                <div class="cap-bar-committed" [style.width.%]="capacity()!.ram_mb_total ? (capacity()!.ram_mb_committed / capacity()!.ram_mb_total * 100) : 0"></div>
              </div>
              <div class="cap-avail">{{ (capacity()!.ram_mb_available / 1024) | number:'1.0-0' }}GB available</div>
            </div>
            <div class="cap-bar-group">
              <div class="cap-label">
                <mat-icon>storage</mat-icon> Storage
                <span class="cap-numbers">{{ capacity()!.disk_gb_committed }}GB / {{ capacity()!.disk_gb_total }}GB</span>
              </div>
              <div class="cap-bar-bg">
                <div class="cap-bar-committed" [style.width.%]="capacity()!.disk_gb_total ? (capacity()!.disk_gb_committed / capacity()!.disk_gb_total * 100) : 0"></div>
              </div>
              <div class="cap-avail">{{ capacity()!.disk_gb_available }}GB available</div>
            </div>
          </div>
        }
      </div>

      <!-- ──── DEPLOYMENT CALCULATOR ────────────────────────────────────────────────────── -->
      <h2 class="section-heading"><mat-icon>calculate</mat-icon> What Can You Deploy?</h2>
      <div class="deploy-calc">
        <div class="profile-cards">
          @for (p of deploymentProfiles; track p.name) {
            <mat-card class="profile-card" [class.cant-deploy]="!canDeploy(p)">
              <div class="profile-icon">
                <mat-icon>{{ p.icon }}</mat-icon>
              </div>
              <div class="profile-info">
                <div class="profile-name">{{ p.name }}</div>
                <div class="profile-spec">{{ p.vm_count }} VMs &middot; {{ p.vcpu_per_vm * p.vm_count }} vCPU &middot; {{ (p.ram_mb_per_vm * p.vm_count / 1024) | number:'1.0-0' }}GB RAM</div>
                <div class="profile-fit" [class.fits]="canDeploy(p)" [class.no-fit]="!canDeploy(p)">
                  @if (canDeploy(p)) {
                    <mat-icon class="sm-icon">check_circle</mat-icon> {{ maxInstances(p) }}x can deploy now
                  } @else {
                    <mat-icon class="sm-icon">block</mat-icon> Insufficient resources
                  }
                </div>
              </div>
            </mat-card>
          }
        </div>
      </div>

      <!-- ──── EVENT SCHEDULER ────────────────────────────────────────────────────────────────── -->
      <h2 class="section-heading"><mat-icon>event</mat-icon> Scheduled Events</h2>
      <div class="scheduler-panel">
        <!-- New event form -->
        <mat-card class="new-event-card">
          <div class="event-form-title">
            <mat-icon>add_circle</mat-icon> Schedule New Event
          </div>
          <div class="event-form">
            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>Event Name</mat-label>
              <input matInput [(ngModel)]="newEvtName" placeholder="e.g. Red Team Exercise Alpha">
            </mat-form-field>
            <div class="event-form-row">
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Start</mat-label>
                <input matInput type="datetime-local" [(ngModel)]="newEvtStart">
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>End</mat-label>
                <input matInput type="datetime-local" [(ngModel)]="newEvtEnd">
              </mat-form-field>
            </div>
            <div class="event-form-row four-col">
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>VMs</mat-label>
                <input matInput type="number" [(ngModel)]="newEvtVms">
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>vCPU</mat-label>
                <input matInput type="number" [(ngModel)]="newEvtCpu" placeholder="vCPU count">
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>RAM</mat-label>
                <input matInput type="number" [(ngModel)]="newEvtRam" placeholder="MB">
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Disk</mat-label>
                <input matInput type="number" [(ngModel)]="newEvtDisk" placeholder="GB">
              </mat-form-field>
            </div>
            <div class="event-form-actions">
              <button mat-stroked-button (click)="checkNewEvent()" [disabled]="!newEvtName">
                <mat-icon>fact_check</mat-icon> Check Fit
              </button>
              <button mat-flat-button color="primary" (click)="scheduleEvent()" [disabled]="!newEvtName || !newEvtStart || !newEvtEnd">
                <mat-icon>event_available</mat-icon> Schedule
              </button>
            </div>
            @if (fitCheckResult()) {
              <div class="fit-result" [class.fit-ok]="fitCheckResult()!.fits" [class.fit-fail]="!fitCheckResult()!.fits">
                <mat-icon>{{ fitCheckResult()!.fits ? 'check_circle' : 'warning' }}</mat-icon>
                {{ fitCheckResult()!.message }}
                @if (!fitCheckResult()!.fits) {
                  <div class="fit-detail">
                    Available: {{ fitCheckResult()!.vcpu_available }} vCPU,
                    {{ (fitCheckResult()!.ram_mb_available / 1024) | number:'1.0-0' }}GB RAM,
                    {{ fitCheckResult()!.disk_gb_available }}GB disk
                  </div>
                }
              </div>
            }
          </div>
        </mat-card>

        <!-- Event timeline -->
        <div class="event-timeline">
          @for (evt of scheduledEvents(); track evt.id) {
            <mat-card class="event-card" [class]="'evt-' + evt.state">
              <div class="event-card-top">
                <div class="event-state-dot" [class]="'dot-' + evt.state"></div>
                <div class="event-card-name">{{ evt.name }}</div>
                <button mat-icon-button class="evt-delete" (click)="deleteEvent(evt.id)"
                        matTooltip="Cancel event">
                  <mat-icon>close</mat-icon>
                </button>
              </div>
              <div class="event-time">
                <mat-icon class="sm-icon">schedule</mat-icon>
                {{ evt.start_time | date:'MMM d, h:mm a' }} – {{ evt.end_time | date:'MMM d, h:mm a' }}
              </div>
              <div class="event-resources">
                <span><mat-icon class="sm-icon">computer</mat-icon> {{ evt.vm_count }} VMs</span>
                <span><mat-icon class="sm-icon">developer_board</mat-icon> {{ evt.vcpu_total }} vCPU</span>
                <span><mat-icon class="sm-icon">memory</mat-icon> {{ (evt.ram_mb_total / 1024) | number:'1.0-0' }}GB</span>
                <span><mat-icon class="sm-icon">storage</mat-icon> {{ evt.disk_gb_total }}GB</span>
              </div>
            </mat-card>
          }
          @if (scheduledEvents().length === 0) {
            <div class="no-events">
              <mat-icon>event_busy</mat-icon>
              <p>No events scheduled</p>
            </div>
          }
        </div>
      </div>

      <!-- ──── RECENT ACTIVITY ────────────────────────────────────────────────────────────────── -->
      <div class="recent-row">
        <div class="recent-col">
          <h2 class="section-heading"><mat-icon>dns</mat-icon> Recent Ranges</h2>
          <div class="card-grid">
            @for (range of ranges(); track range.id) {
              <mat-card>
                <mat-card-header>
                  <mat-icon mat-card-avatar>dns</mat-icon>
                  <mat-card-title>{{ range.name }}</mat-card-title>
                  <mat-card-subtitle>
                    <span class="status-chip" [class]="range.state">{{ range.state }}</span>
                  </mat-card-subtitle>
                </mat-card-header>
                <mat-card-actions>
                  <button mat-button [routerLink]="['/ranges']">View</button>
                </mat-card-actions>
              </mat-card>
            }
            @empty {
              <mat-card><mat-card-content>No ranges yet. <a routerLink="/ranges">Create one</a></mat-card-content></mat-card>
            }
          </div>
        </div>
        <div class="recent-col">
          <h2 class="section-heading"><mat-icon>fitness_center</mat-icon> Recent Exercises</h2>
          <div class="card-grid">
            @for (ex of exercises(); track ex.id) {
              <mat-card>
                <mat-card-header>
                  <mat-icon mat-card-avatar>fitness_center</mat-icon>
                  <mat-card-title>{{ ex.name }}</mat-card-title>
                  <mat-card-subtitle>
                    <span class="status-chip" [class]="ex.state">{{ ex.state }}</span>
                  </mat-card-subtitle>
                </mat-card-header>
                <mat-card-actions>
                  <button mat-button [routerLink]="['/exercises']">View</button>
                </mat-card-actions>
              </mat-card>
            }
            @empty {
              <mat-card><mat-card-content>No exercises yet.</mat-card-content></mat-card>
            }
          </div>
        </div>
      </div>
      <!-- ── PERSONAL DASHBOARD ────────────────────────── -->
      <mat-divider style="margin: 32px 0;"></mat-divider>
      <h2 class="section-heading"><mat-icon>person</mat-icon> My Training Dashboard</h2>

      <div class="personal-row">
        <mat-card class="personal-card">
          <mat-card-header>
            <mat-icon mat-card-avatar>school</mat-icon>
            <mat-card-title>Active Courses</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <div class="big-number">{{ myActiveCourses }}</div>
            <p class="card-note">courses in progress</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-button routerLink="/training">Go to Training</button>
          </mat-card-actions>
        </mat-card>

        <mat-card class="personal-card">
          <mat-card-header>
            <mat-icon mat-card-avatar>trending_up</mat-icon>
            <mat-card-title>Competency Score</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <div class="big-number accent-text">{{ myCompetencyPct }}%</div>
            <mat-progress-bar mode="determinate" [value]="myCompetencyPct"></mat-progress-bar>
          </mat-card-content>
          <mat-card-actions>
            <button mat-button routerLink="/my-progress">View Progress</button>
          </mat-card-actions>
        </mat-card>

        <mat-card class="personal-card">
          <mat-card-header>
            <mat-icon mat-card-avatar>emoji_events</mat-icon>
            <mat-card-title>Certifications</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <div class="big-number">{{ myCertCount }}</div>
            <p class="card-note">active certifications</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-button routerLink="/competency">View Framework</button>
          </mat-card-actions>
        </mat-card>

        <mat-card class="personal-card">
          <mat-card-header>
            <mat-icon mat-card-avatar>public</mat-icon>
            <mat-card-title>Coalition Status</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <div class="big-number">{{ nationCount }} nations</div>
            <p class="card-note">{{ coalitionCount }} coalitions active</p>
          </mat-card-content>
          <mat-card-actions>
            <button mat-button routerLink="/users">View Directory</button>
          </mat-card-actions>
        </mat-card>
      </div>

      <!-- Quick Actions -->
      <h2 class="section-heading"><mat-icon>bolt</mat-icon> Quick Actions</h2>
      <div class="quick-actions">
        <button mat-stroked-button routerLink="/range-designer"><mat-icon>architecture</mat-icon> Design Range</button>
        <button mat-stroked-button routerLink="/scenario-builder"><mat-icon>build</mat-icon> Build Scenario</button>
        <button mat-stroked-button routerLink="/training"><mat-icon>school</mat-icon> Browse Courses</button>
        <button mat-stroked-button routerLink="/infrastructure"><mat-icon>dns</mat-icon> Manage Infra</button>
        <button mat-stroked-button routerLink="/ai-orchestrator"><mat-icon>psychology</mat-icon> AI Config</button>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; }
    .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    .stat-card mat-card-content { text-align: center; padding: 24px; }
    .stat-card mat-icon { font-size: 40px; width: 40px; height: 40px; color: var(--accent); }
    .stat-value { font-size: 28px; font-weight: 600; margin: 8px 0 4px; color: var(--text-primary); }
    .stat-label { color: var(--text-secondary); }
    @media (max-width: 960px) { .stats-row { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 640px) { .stats-row { grid-template-columns: 1fr; } }

    .section-heading {
      display: flex; align-items: center; gap: 8px; margin: 28px 0 14px;
      font-size: 18px; font-weight: 600; color: var(--text-primary);
    }
    .section-heading mat-icon { color: var(--accent); font-size: 22px; width: 22px; height: 22px; }

    /* ──── Cluster Capacity Panel ────────────────────────────────────────── */
    .cluster-panel { display: flex; gap: 20px; flex-wrap: wrap; }
    .node-cards { display: flex; gap: 16px; flex: 1; min-width: 320px; flex-wrap: wrap; }
    .node-card {
      flex: 1 1 280px; max-width: 420px; padding: 16px;
      border-left: 4px solid var(--success);
    }
    .node-card.offline { border-left-color: var(--alert); opacity: 0.7; }
    .node-header {
      display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
    }
    .node-name { font-weight: 700; font-size: 16px; color: var(--text-primary); }
    .node-ip { color: var(--text-muted); font-size: 12px; font-family: 'Consolas', monospace; }
    .gauge-row { display: flex; flex-direction: column; gap: 8px; }
    .gauge-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; }
    .gauge-bar-bg {
      height: 10px; border-radius: 5px; background: var(--bg-primary);
      overflow: hidden; border: 1px solid var(--border);
    }
    .gauge-bar-fill {
      height: 100%; border-radius: 5px; background: var(--accent);
      transition: width 0.6s ease;
    }
    .gauge-bar-fill.warn { background: #FFA726; }
    .gauge-bar-fill.crit { background: var(--alert); }
    .gauge-val { font-size: 12px; color: var(--text-secondary); }
    .node-footer {
      display: flex; gap: 12px; margin-top: 12px; padding-top: 10px;
      border-top: 1px solid var(--border); color: var(--text-muted); font-size: 12px;
    }
    .node-footer span { display: flex; align-items: center; gap: 3px; }
    .sm-icon { font-size: 15px !important; width: 15px !important; height: 15px !important; }
    .empty-card { text-align: center; padding: 40px; color: var(--text-muted); }
    .empty-big { font-size: 48px; width: 48px; height: 48px; color: var(--border-light); margin-bottom: 12px; }

    @keyframes spin { to { transform: rotate(360deg); } }
    .spin { animation: spin 1.2s linear infinite; }

    /* ──── Aggregate capacity bars ────────────────────────────────────── */
    .capacity-aggregate {
      flex: 0 0 320px; display: flex; flex-direction: column; gap: 16px;
      padding: 16px; background: var(--bg-secondary); border-radius: 12px;
      border: 1px solid var(--border);
    }
    .cap-bar-group { display: flex; flex-direction: column; gap: 4px; }
    .cap-label {
      display: flex; align-items: center; gap: 6px; font-size: 13px;
      font-weight: 600; color: var(--text-primary);
    }
    .cap-label mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--accent); }
    .cap-numbers { margin-left: auto; font-weight: 400; color: var(--text-muted); font-size: 12px; }
    .cap-bar-bg {
      height: 14px; border-radius: 7px; background: var(--bg-primary);
      border: 1px solid var(--border); overflow: hidden;
    }
    .cap-bar-committed {
      height: 100%; border-radius: 7px;
      background: linear-gradient(90deg, var(--accent), #1E88E5);
      transition: width 0.6s ease;
    }
    .cap-avail { font-size: 12px; color: var(--success); font-weight: 500; }

    /* ──── Deployment Calculator ──────────────────────────────────────────── */
    .deploy-calc { margin-bottom: 8px; }
    .profile-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
    .profile-card {
      display: flex; align-items: center; gap: 14px; padding: 16px;
      border-left: 4px solid var(--success); transition: all 0.2s;
    }
    .profile-card.cant-deploy { border-left-color: var(--alert); opacity: 0.65; }
    .profile-icon mat-icon { font-size: 32px; width: 32px; height: 32px; color: var(--accent); }
    .profile-name { font-weight: 600; font-size: 15px; color: var(--text-primary); }
    .profile-spec { font-size: 12px; color: var(--text-muted); margin: 2px 0; }
    .profile-fit { display: flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 500; }
    .profile-fit.fits { color: var(--success); }
    .profile-fit.no-fit { color: var(--alert); }

    /* ──── Event Scheduler ────────────────────────────────────────────────────── */
    .scheduler-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    @media (max-width: 1200px) { .scheduler-panel { grid-template-columns: 1fr; } }
    .new-event-card { padding: 20px; }
    .event-form-title {
      display: flex; align-items: center; gap: 8px; font-size: 15px;
      font-weight: 600; color: var(--accent); margin-bottom: 14px;
    }
    .event-form { display: flex; flex-direction: column; gap: 10px; }
    .event-form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .event-form-row.four-col { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
    @media (max-width: 1000px) { .event-form-row.four-col { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 768px) { .event-form-row.four-col { grid-template-columns: 1fr; } }
    .event-form mat-form-field { width: 100%; font-size: 13px; }
    .event-form-actions { display: flex; gap: 10px; }
    .full-width { width: 100%; }
    .fit-result {
      display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
      padding: 10px 14px; border-radius: 8px; font-size: 13px; font-weight: 500;
    }
    .fit-ok { background: rgba(76, 175, 80, 0.12); color: var(--success); }
    .fit-fail { background: rgba(244, 67, 54, 0.12); color: var(--alert); }
    .fit-detail { width: 100%; font-size: 12px; font-weight: 400; margin-top: 4px; }

    /* ──── Event timeline ──────────────────────────────────────────────────────── */
    .event-timeline { display: flex; flex-direction: column; gap: 12px; }
    .event-card {
      padding: 14px 16px; border-left: 4px solid var(--accent);
      position: relative;
    }
    .event-card.evt-active { border-left-color: var(--success); }
    .event-card.evt-completed { border-left-color: var(--text-muted); opacity: 0.6; }
    .event-card.evt-cancelled { border-left-color: var(--alert); opacity: 0.5; text-decoration: line-through; }
    .event-card-top { display: flex; align-items: center; gap: 8px; }
    .event-state-dot {
      width: 8px; height: 8px; border-radius: 50%;
    }
    .dot-scheduled { background: var(--accent); }
    .dot-active { background: var(--success); }
    .dot-completed { background: var(--text-muted); }
    .dot-cancelled { background: var(--alert); }
    .dot-draft { background: var(--border-light); }
    .event-card-name { font-weight: 600; font-size: 14px; color: var(--text-primary); flex: 1; }
    .evt-delete { opacity: 0.5; }
    .evt-delete:hover { opacity: 1; }
    .event-time { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-muted); margin: 4px 0; }
    .event-resources { display: flex; gap: 14px; font-size: 12px; color: var(--text-secondary); }
    .event-resources span { display: flex; align-items: center; gap: 3px; }
    .no-events { text-align: center; padding: 40px; color: var(--text-muted); }
    .no-events mat-icon { font-size: 40px; width: 40px; height: 40px; color: var(--border-light); }

    /* ──── Recent Activity ────────────────────────────────────────────────────── */
    .recent-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 8px; }
    @media (max-width: 960px) { .recent-row { grid-template-columns: 1fr; } }
.error-text { color: var(--alert); font-size: 13px; }
    /* -- Personal Dashboard ---------------------- */
    .personal-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    @media (max-width: 960px) { .personal-row { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 640px) { .personal-row { grid-template-columns: 1fr; } }
    .personal-card mat-card-content { text-align: center; padding: 16px 0; }
    .big-number { font-size: 36px; font-weight: 700; color: var(--text-primary); }
    .accent-text { color: var(--accent) !important; }
    .card-note { font-size: 13px; color: var(--text-secondary); margin: 4px 0 0; }
    .quick-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 32px; }
    .quick-actions button mat-icon { margin-right: 6px; }
  `],
})
export class DashboardComponent implements OnInit {
  health = signal<HealthResponse | null>(null);
  ranges = signal<Range[]>([]);
  exercises = signal<Exercise[]>([]);
  rangeCount = signal(0);
  exerciseCount = signal(0);

  clusterNodes = signal<ClusterNode[]>([]);
  clusterLoading = signal(false);
  capacity = signal<CapacityInfo | null>(null);
  scheduledEvents = signal<ScheduledEvent[]>([]);
  fitCheckResult = signal<CapacityInfo | null>(null);

  /* New event form */
  newEvtName = '';
  newEvtStart = '';
  newEvtEnd = '';
  newEvtVms = 10;
  newEvtCpu = 40;
  newEvtRam = 81920;
  newEvtDisk = 500;

  /* Personal dashboard */
  myActiveCourses = 0;
  myCompetencyPct = 0;
  myCertCount = 0;
  nationCount = 0;
  coalitionCount = 0;

  /* Deployment profile presets (SimSpace-style) */
  deploymentProfiles: DeploymentProfile[] = [
    { name: 'Small Enterprise', icon: 'business', vm_count: 15, vcpu_per_vm: 2, ram_mb_per_vm: 4096, disk_gb_per_vm: 60 },
    { name: 'SOC Training Lab', icon: 'shield', vm_count: 25, vcpu_per_vm: 2, ram_mb_per_vm: 4096, disk_gb_per_vm: 40 },
    { name: 'Red vs Blue', icon: 'sports_mma', vm_count: 40, vcpu_per_vm: 2, ram_mb_per_vm: 4096, disk_gb_per_vm: 50 },
    { name: 'Full Enterprise', icon: 'domain', vm_count: 80, vcpu_per_vm: 4, ram_mb_per_vm: 8192, disk_gb_per_vm: 100 },
    { name: 'ICS / SCADA', icon: 'precision_manufacturing', vm_count: 20, vcpu_per_vm: 2, ram_mb_per_vm: 2048, disk_gb_per_vm: 30 },
    { name: 'Cloud Range (Multi-Tenant)', icon: 'cloud', vm_count: 200, vcpu_per_vm: 2, ram_mb_per_vm: 4096, disk_gb_per_vm: 60 },
  ];

  constructor(private api: ApiService, private http: HttpClient, private snack: MatSnackBar, private cdr: ChangeDetectorRef) {}

  ngOnInit(): void {
    // Core data
    this.api.health().subscribe({ next: h => this.health.set(h), error: () => {} });
    this.api.listRanges(5).subscribe({
      next: r => {
        this.ranges.set(r);
        this.rangeCount.set(r.filter(x => ['provisioning', 'ready', 'running'].includes(x.state)).length);
      },
      error: () => {},
    });
    this.api.listExercises(5).subscribe({
      next: e => { this.exercises.set(e); this.exerciseCount.set(e.length); },
      error: () => {},
    });

    // Cluster discovery
    this.refreshCluster();

    // Capacity & events
    this.api.getCapacity().subscribe({ next: c => this.capacity.set(c), error: () => {} });
    this.loadEvents();

    // Personal dashboard data
    this.http.get<any[]>('/api/directory/nations').subscribe({
      next: n => this.nationCount = n.length,
      error: () => {},
    });
    this.http.get<any[]>('/api/directory/coalitions').subscribe({
      next: c => this.coalitionCount = c.length,
      error: () => {},
    });
  }

  refreshCluster(): void {
    this.clusterLoading.set(true);
    this.api.proxmoxDiscover().subscribe({
      next: (res: any) => {
        this.clusterNodes.set(res.cluster || []);
        this.clusterLoading.set(false);
      },
      error: () => {
        this.clusterNodes.set([]);
        this.clusterLoading.set(false);
      },
    });
  }

  loadEvents(): void {
    this.api.listScheduledEvents().subscribe({
      next: (res: any) => this.scheduledEvents.set(Array.isArray(res) ? res : (res.items || [])),
      error: () => {},
    });
  }

  healthTooltip(): string {
    const h = this.health();
    if (!h) return '';
    return `DB: ${h.db ? 'connected' : 'down'} | Redis: ${h.redis ? 'connected' : 'down'}`;
  }

  /* Deployment calculator */
  canDeploy(p: DeploymentProfile): boolean {
    const c = this.capacity();
    if (!c) return false;
    return (p.vcpu_per_vm * p.vm_count <= c.vcpu_available) &&
           (p.ram_mb_per_vm * p.vm_count <= c.ram_mb_available) &&
           (p.disk_gb_per_vm * p.vm_count <= c.disk_gb_available);
  }

  maxInstances(p: DeploymentProfile): number {
    const c = this.capacity();
    if (!c) return 0;
    const totalCpu = p.vcpu_per_vm * p.vm_count;
    const totalRam = p.ram_mb_per_vm * p.vm_count;
    const totalDisk = p.disk_gb_per_vm * p.vm_count;
    if (totalCpu === 0 || totalRam === 0 || totalDisk === 0) return 0;
    return Math.min(
      Math.floor(c.vcpu_available / totalCpu),
      Math.floor(c.ram_mb_available / totalRam),
      Math.floor(c.disk_gb_available / totalDisk),
    );
  }

  /* Event scheduling */
  checkNewEvent(): void {
    const body = {
      start_time: this.newEvtStart ? new Date(this.newEvtStart).toISOString() : new Date().toISOString(),
      end_time: this.newEvtEnd ? new Date(this.newEvtEnd).toISOString() : new Date().toISOString(),
      vcpu_needed: this.newEvtCpu,
      ram_mb_needed: this.newEvtRam,
      disk_gb_needed: this.newEvtDisk,
    };
    this.api.checkCapacity(body).subscribe({
      next: (res: any) => {
        this.fitCheckResult.set(res);
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.snack.open('Capacity check failed', 'OK', { duration: 3000 });
      },
    });
  }

  scheduleEvent(): void {
    const body = {
      name: this.newEvtName,
      start_time: this.newEvtStart ? new Date(this.newEvtStart).toISOString() : new Date().toISOString(),
      end_time: this.newEvtEnd ? new Date(this.newEvtEnd).toISOString() : new Date().toISOString(),
      vm_count: this.newEvtVms,
      vcpu_total: this.newEvtCpu,
      ram_mb_total: this.newEvtRam,
      disk_gb_total: this.newEvtDisk,
    };
    this.api.createScheduledEvent(body).subscribe({
      next: () => {
        this.snack.open('Event scheduled', '', { duration: 2000, panelClass: 'snack-success' });
        this.newEvtName = '';
        this.fitCheckResult.set(null);
        this.loadEvents();

      },
      error: (err: any) => {
        const msg = err.error?.detail || 'Failed to schedule event';
        this.snack.open(msg, 'OK', { duration: 5000 });
      },
    });
  }

  deleteEvent(id: string): void {
    this.api.deleteScheduledEvent(id).subscribe({
      next: () => {
        this.snack.open('Event cancelled', '', { duration: 2000 });
        this.loadEvents();

      },
      error: () => this.snack.open('Failed to delete', 'OK', { duration: 3000 }),
    });
  }
}
