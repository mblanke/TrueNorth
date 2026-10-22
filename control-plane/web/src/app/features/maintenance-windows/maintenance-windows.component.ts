import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import {
  MaintenanceWindow, MaintenanceWindowIn, MaintenanceWindowsApiService, WindowScope,
} from '@core/services/maintenance-windows-api.service';
import { NotificationService } from '@core/services/notification.service';
import type { RangeSummary } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { apiError } from '../ranges/range-capabilities';

interface Form {
  id?: string;
  reason: string;
  start: string; // datetime-local, the browser's time zone
  end: string;
  scope: WindowScope;
  rangeIds: string[];
  tags: string;
  platformWide: boolean;
}

/** A datetime-local value ("2026-11-01T22:00") in the browser's zone, as an instant (UTC, with Z). */
export function localToIso(local: string): string {
  return new Date(local).toISOString();
}

/** An instant as a datetime-local value in the browser's zone. */
export function isoToLocal(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The request body for a form, or why it cannot be sent yet. */
export function toBody(f: Form): MaintenanceWindowIn | string {
  if (f.reason.trim().length < 3) return 'Give a reason (at least 3 characters).';
  if (!f.start || !f.end) return 'Set a start and an end.';
  if (new Date(f.end) <= new Date(f.start)) return 'The end must be after the start.';
  const tags = f.tags.split(',').map(t => t.trim()).filter(Boolean);
  if (f.scope === 'ranges' && !f.rangeIds.length) return 'Choose at least one range.';
  if (f.scope === 'tags' && !tags.length) return 'Give at least one tag.';
  return {
    reason: f.reason.trim(),
    starts_at: localToIso(f.start),
    ends_at: localToIso(f.end),
    scope: f.scope,
    range_ids: f.scope === 'ranges' ? f.rangeIds : [],
    tags: f.scope === 'tags' ? tags : [],
    platform_wide: f.platformWide,
  };
}

/**
 * Maintenance windows (ADR 0013 §7, range certification S7): an administrator's list,
 * create, edit and delete. Inside a window the API refuses provision, reset/restore and
 * normal destroy of the ranges it covers; automated cleanup and emergency teardown still run.
 */
@Component({
  selector: 'tn-maintenance-windows',
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatCardModule, MatCheckboxModule, MatFormFieldModule,
    MatIconModule, MatInputModule, MatSelectModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">construction</mat-icon>
          <div>
            <h1>Maintenance windows</h1>
            <p class="subtitle">While one is in effect, provisioning, resets and destroys of the ranges it covers are
              refused. Automated cleanup and emergency teardown still run. Times are in your time zone ({{ tz }}).</p>
          </div>
        </div>
        <div class="header-actions">
          <button mat-raised-button color="primary" (click)="startCreate()" [disabled]="!!form()">
            <mat-icon>add</mat-icon> New window
          </button>
        </div>
      </div>

      @if (form(); as f) {
        <mat-card class="mt-2">
          <mat-card-content>
            <h2>{{ f.id ? 'Edit window' : 'New window' }}</h2>
            <div class="form-grid">
              <mat-form-field appearance="outline" class="wide">
                <mat-label>Reason (shown in every refusal)</mat-label>
                <input matInput [(ngModel)]="f.reason" maxlength="500" placeholder="vCenter 8.0U3 upgrade">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Starts</mat-label>
                <input matInput type="datetime-local" [(ngModel)]="f.start">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Ends</mat-label>
                <input matInput type="datetime-local" [(ngModel)]="f.end">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Covers</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="f.scope">
                  <mat-option value="all">All ranges</mat-option>
                  <mat-option value="ranges">Specific ranges</mat-option>
                  <mat-option value="tags">Ranges with tags</mat-option>
                </mat-select>
              </mat-form-field>
              @if (f.scope === 'ranges') {
                <mat-form-field appearance="outline" class="wide">
                  <mat-label>Ranges</mat-label>
                  <mat-select panelClass="tn-select-panel" multiple [(ngModel)]="f.rangeIds">
                    @for (r of ranges(); track r.id) { <mat-option [value]="r.id">{{ r.name }}</mat-option> }
                  </mat-select>
                </mat-form-field>
              }
              @if (f.scope === 'tags') {
                <mat-form-field appearance="outline" class="wide">
                  <mat-label>Tags (comma-separated; a range template's top-level tags)</mat-label>
                  <input matInput [(ngModel)]="f.tags" placeholder="soc, blue-team">
                </mat-form-field>
              }
              @if (canPlatform() && !f.id) {
                <mat-checkbox [(ngModel)]="f.platformWide">Every organisation (platform-wide)</mat-checkbox>
              }
            </div>
            @if (formError()) { <p class="error-text" role="alert">{{ formError() }}</p> }
            <button mat-raised-button color="primary" (click)="save()" [disabled]="busy()">
              {{ busy() ? 'Saving...' : 'Save' }}
            </button>
            <button mat-button (click)="form.set(null)" [disabled]="busy()">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      <div class="mt-2 toolbar">
        <mat-checkbox [ngModel]="includePast()" (ngModelChange)="includePast.set($event); load()">
          Show windows that have ended
        </mat-checkbox>
      </div>

      @if (error()) {
        <p class="error-text" role="alert">{{ error() }}</p>
      } @else if (windows().length === 0) {
        <tn-empty-state icon="event_available" title="No maintenance windows"
          message="Ranges can be provisioned, reset and destroyed at any time." />
      } @else {
        <table class="tn-table full-width">
          <caption class="sr-only">Maintenance windows</caption>
          <thead><tr>
            <th scope="col">When</th><th scope="col">Reason</th><th scope="col">Covers</th>
            <th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th>
          </tr></thead>
          <tbody>
            @for (w of windows(); track w.id) {
              <tr>
                <td>{{ w.starts_at | date:'medium' }} to {{ w.ends_at | date:'medium' }}</td>
                <td>{{ w.reason }}</td>
                <td>{{ covers(w) }}{{ w.platform_wide ? ' · all organisations' : '' }}</td>
                <td><span class="status-chip" [class.running]="w.active">{{ status(w) }}</span></td>
                <td class="actions">
                  @if (w.can_edit) {
                    <button mat-button (click)="edit(w)" [disabled]="busy()">Edit</button>
                    @if (confirmDelete() === w.id) {
                      <button mat-button color="warn" (click)="remove(w)" [disabled]="busy()">Delete now</button>
                      <button mat-button (click)="confirmDelete.set(null)">Keep</button>
                    } @else {
                      <button mat-button color="warn" (click)="confirmDelete.set(w.id)" [disabled]="busy()">Delete</button>
                    }
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      }
    </div>
  `,
  styles: [
    `
      .form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 16px; align-items: center; }
      .form-grid .wide { grid-column: 1 / -1; }
      .toolbar { display: flex; justify-content: flex-end; }
      td.actions { white-space: nowrap; text-align: right; }
    `,
  ],
})
export class MaintenanceWindowsComponent implements OnInit {
  private readonly api = inject(MaintenanceWindowsApiService);
  private readonly core = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);

  readonly tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  readonly windows = signal<MaintenanceWindow[]>([]);
  readonly ranges = signal<RangeSummary[]>([]);
  readonly form = signal<Form | null>(null);
  readonly formError = signal<string | null>(null);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly includePast = signal(false);
  readonly confirmDelete = signal<string | null>(null);
  readonly canPlatform = computed(() => this.auth.isPlatformAdmin());

  ngOnInit(): void {
    this.load();
    this.core.listRanges(200).subscribe({ next: r => this.ranges.set(r), error: () => this.ranges.set([]) });
  }

  load(): void {
    this.api.list(this.includePast()).subscribe({
      next: page => { this.windows.set(page.items); this.error.set(null); },
      error: err => this.error.set(apiError(err)),
    });
  }

  startCreate(): void {
    const start = new Date(Date.now() + 60 * 60 * 1000);
    start.setMinutes(0, 0, 0);
    const end = new Date(start.getTime() + 2 * 60 * 60 * 1000);
    this.formError.set(null);
    this.form.set({
      reason: '', start: isoToLocal(start.toISOString()), end: isoToLocal(end.toISOString()),
      scope: 'all', rangeIds: [], tags: '', platformWide: false,
    });
  }

  edit(w: MaintenanceWindow): void {
    this.formError.set(null);
    this.form.set({
      id: w.id, reason: w.reason, start: isoToLocal(w.starts_at), end: isoToLocal(w.ends_at), scope: w.scope,
      rangeIds: [...w.range_ids], tags: w.tags.join(', '), platformWide: w.platform_wide,
    });
  }

  save(): void {
    const f = this.form();
    if (!f) return;
    const body = toBody(f);
    if (typeof body === 'string') { this.formError.set(body); return; }
    this.busy.set(true);
    const call = f.id ? this.api.update(f.id, body) : this.api.create(body);
    call.subscribe({
      next: w => {
        this.busy.set(false);
        this.form.set(null);
        this.notify.success(`${f.id ? 'Updated' : 'Created'}: ${w.reason}`);
        this.load();
      },
      error: err => { this.busy.set(false); this.formError.set(apiError(err)); },
    });
  }

  remove(w: MaintenanceWindow): void {
    this.busy.set(true);
    this.api.remove(w.id).subscribe({
      next: () => { this.busy.set(false); this.confirmDelete.set(null); this.notify.success('Window deleted'); this.load(); },
      error: err => { this.busy.set(false); this.notify.error(apiError(err)); },
    });
  }

  covers(w: MaintenanceWindow): string {
    if (w.scope === 'ranges') {
      const names = w.range_ids.map(id => this.ranges().find(r => r.id === id)?.name ?? id.slice(0, 8));
      return names.length ? names.join(', ') : 'Specific ranges';
    }
    if (w.scope === 'tags') return `Tagged ${w.tags.join(', ')}`;
    return 'All ranges';
  }

  status(w: MaintenanceWindow): string {
    if (w.active) return 'In effect';
    return new Date(w.ends_at) <= new Date() ? 'Ended' : 'Upcoming';
  }
}
