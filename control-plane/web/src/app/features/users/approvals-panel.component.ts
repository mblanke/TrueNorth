import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar } from '@angular/material/snack-bar';
import { environment } from '@env/environment';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

interface RegistrationRequest {
  id: string;
  email: string;
  display_name: string;
  rank: string | null;
  unit: string | null;
  callsign: string | null;
  ad_groups: string;
  ad_distinguished_name: string | null;
  requested_cohort: string | null;
  justification: string | null;
  suggested_role: string | null;
  suggested_tenant_id: string | null;
  status: string;
  submitted_at: string | null;
  decision_reason: string | null;
}

const ROLES = ['student', 'instructor', 'observer', 'range_ops', 'admin'];

/**
 * The instructor's intake queue.
 *
 * Its own component rather than another tab body inside users.component.ts,
 * which is already ~600 lines.
 *
 * The suggested role is shown as a hint, never pre-applied silently — an AD
 * group can propose `admin`, and it is still a person who decides.
 */
@Component({
  selector: 'tn-approvals-panel',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatTableModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatCheckboxModule,
    MatTooltipModule,
    MatProgressSpinnerModule,
    EmptyStateComponent,
  ],
  template: `
    <div class="panel">
      <header class="bar">
        <mat-form-field appearance="outline" class="filter">
          <mat-label>Status</mat-label>
          <mat-select [(ngModel)]="statusFilter" (ngModelChange)="load()">
            <mat-option value="pending">Pending</mat-option>
            <mat-option value="approved">Approved</mat-option>
            <mat-option value="rejected">Rejected</mat-option>
            <mat-option value="all">All</mat-option>
          </mat-select>
        </mat-form-field>

        <mat-form-field appearance="outline" class="filter">
          <mat-label>Cohort</mat-label>
          <input matInput [(ngModel)]="cohortFilter" (change)="load()" placeholder="any" />
        </mat-form-field>

        <span class="spacer"></span>
        <button mat-stroked-button (click)="load()">
          <mat-icon>refresh</mat-icon> Refresh
        </button>
      </header>

      @if (selected().length) {
        <mat-card class="bulk">
          <div class="bulk-row">
            <strong>{{ selected().length }} selected</strong>
            <mat-form-field appearance="outline" class="filter">
              <mat-label>Role to assign</mat-label>
              <mat-select [(ngModel)]="bulkRole">
                @for (r of roles; track r) {
                  <mat-option [value]="r">{{ label(r) }}</mat-option>
                }
              </mat-select>
            </mat-form-field>
            <button mat-flat-button color="primary" [disabled]="busy()" (click)="bulkApprove()">
              Approve {{ selected().length }}
            </button>
          </div>
          <p class="hint">
            Everyone selected receives the same role. Approving is what creates their account.
          </p>
        </mat-card>
      }

      @if (loading()) {
        <div class="centred"><mat-spinner diameter="32" /></div>
      } @else if (!requests().length) {
        <tn-empty-state
          icon="how_to_reg"
          title="Nothing waiting"
          message="Registration requests appear here when someone signs in with their directory account and asks for access." />
      } @else {
        <table mat-table [dataSource]="requests()" class="full">
          <ng-container matColumnDef="select">
            <th mat-header-cell *matHeaderCellDef></th>
            <td mat-cell *matCellDef="let r">
              @if (r.status === 'pending') {
                <mat-checkbox
                  [checked]="isSelected(r.id)"
                  (change)="toggle(r.id)" />
              }
            </td>
          </ng-container>

          <ng-container matColumnDef="person">
            <th mat-header-cell *matHeaderCellDef>Person</th>
            <td mat-cell *matCellDef="let r">
              <div class="who">
                <strong>{{ r.rank ? r.rank + ' ' : '' }}{{ r.display_name }}</strong>
                <span class="muted">{{ r.email }}</span>
                @if (r.unit) {
                  <span class="muted">{{ r.unit }}</span>
                }
              </div>
            </td>
          </ng-container>

          <ng-container matColumnDef="groups">
            <th mat-header-cell *matHeaderCellDef>AD groups</th>
            <td mat-cell *matCellDef="let r">
              @for (g of groupsOf(r); track g) {
                <mat-chip class="chip">{{ g }}</mat-chip>
              }
            </td>
          </ng-container>

          <ng-container matColumnDef="suggested">
            <th mat-header-cell *matHeaderCellDef>
              Suggested
              <mat-icon
                class="hint-icon"
                matTooltip="Derived from directory groups. A suggestion only — approving is what grants a role.">
                info_outline
              </mat-icon>
            </th>
            <td mat-cell *matCellDef="let r">
              @if (r.suggested_role) {
                <mat-chip class="chip chip--suggest">{{ label(r.suggested_role) }}</mat-chip>
              } @else {
                <span class="muted">—</span>
              }
            </td>
          </ng-container>

          <ng-container matColumnDef="cohort">
            <th mat-header-cell *matHeaderCellDef>Cohort</th>
            <td mat-cell *matCellDef="let r">{{ r.requested_cohort || '—' }}</td>
          </ng-container>

          <ng-container matColumnDef="status">
            <th mat-header-cell *matHeaderCellDef>Status</th>
            <td mat-cell *matCellDef="let r">
              <mat-chip class="chip chip--{{ r.status }}">{{ r.status }}</mat-chip>
            </td>
          </ng-container>

          <ng-container matColumnDef="actions">
            <th mat-header-cell *matHeaderCellDef>Decision</th>
            <td mat-cell *matCellDef="let r">
              @if (r.status === 'pending') {
                <div class="decide">
                  <mat-form-field appearance="outline" class="role-pick">
                    <mat-label>Role</mat-label>
                    <mat-select [(ngModel)]="roleChoice[r.id]">
                      @for (role of roles; track role) {
                        <mat-option [value]="role">{{ label(role) }}</mat-option>
                      }
                    </mat-select>
                  </mat-form-field>
                  <button
                    mat-flat-button
                    color="primary"
                    [disabled]="busy()"
                    (click)="approve(r)">
                    Approve
                  </button>
                  <button mat-stroked-button [disabled]="busy()" (click)="reject(r)">
                    Decline
                  </button>
                </div>
              } @else {
                <span class="muted">{{ r.decision_reason || '—' }}</span>
              }
            </td>
          </ng-container>

          <tr mat-header-row *matHeaderRowDef="columns"></tr>
          <tr mat-row *matRowDef="let row; columns: columns"></tr>
        </table>
      }
    </div>
  `,
  styles: [
    `
      .panel {
        padding: 1rem 0;
      }
      .bar {
        display: flex;
        gap: 0.75rem;
        align-items: center;
        flex-wrap: wrap;
        margin-bottom: 0.5rem;
      }
      .spacer {
        flex: 1;
      }
      .filter {
        min-width: 160px;
      }
      .full {
        width: 100%;
      }
      .centred {
        display: flex;
        justify-content: center;
        padding: 2rem;
      }
      .who {
        display: flex;
        flex-direction: column;
        padding: 0.4rem 0;
      }
      .muted {
        opacity: 0.7;
        font-size: 0.8rem;
      }
      .chip {
        margin: 0.1rem;
        font-size: 0.72rem;
      }
      .chip--suggest {
        background: rgba(63, 81, 181, 0.15);
      }
      .chip--pending {
        background: rgba(255, 167, 38, 0.18);
      }
      .chip--approved {
        background: rgba(76, 175, 80, 0.18);
      }
      .chip--rejected {
        background: rgba(211, 47, 47, 0.15);
      }
      .decide {
        display: flex;
        gap: 0.4rem;
        align-items: center;
        padding: 0.4rem 0;
      }
      .role-pick {
        width: 140px;
      }
      .bulk {
        margin: 0.5rem 0 1rem;
        padding: 0.75rem 1rem;
      }
      .bulk-row {
        display: flex;
        gap: 0.75rem;
        align-items: center;
        flex-wrap: wrap;
      }
      .hint {
        margin: 0.25rem 0 0;
        font-size: 0.78rem;
        opacity: 0.7;
      }
      .hint-icon {
        font-size: 0.95rem;
        width: 0.95rem;
        height: 0.95rem;
        opacity: 0.6;
        vertical-align: middle;
      }
    `,
  ],
})
export class ApprovalsPanelComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly snack = inject(MatSnackBar);

  readonly roles = ROLES;
  columns = ['select', 'person', 'groups', 'suggested', 'cohort', 'status', 'actions'];

  requests = signal<RegistrationRequest[]>([]);
  loading = signal(true);
  busy = signal(false);
  private selectedIds = signal<Set<string>>(new Set());
  selected = computed(() => Array.from(this.selectedIds()));

  statusFilter = 'pending';
  cohortFilter = '';
  bulkRole = 'student';
  roleChoice: Record<string, string> = {};

  ngOnInit(): void {
    this.load();
  }

  label(role: string): string {
    // `student` is the wire value; "Trainee" is what people here call it.
    return role === 'student' ? 'Trainee' : role.replace('_', ' ');
  }

  groupsOf(r: RegistrationRequest): string[] {
    try {
      const parsed = JSON.parse(r.ad_groups || '[]');
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch {
      return [];
    }
  }

  load(): void {
    this.loading.set(true);
    this.selectedIds.set(new Set());
    const params = new URLSearchParams({ status: this.statusFilter });
    if (this.cohortFilter) {
      params.set('cohort', this.cohortFilter);
    }
    this.http
      .get<RegistrationRequest[]>(`${environment.apiUrl}/registration/requests?${params}`)
      .subscribe({
        next: (rows) => {
          this.requests.set(rows ?? []);
          for (const r of rows ?? []) {
            // Default the picker to the suggestion, so the common case is one
            // click — but the value is visible and changeable before approval.
            this.roleChoice[r.id] ??= r.suggested_role ?? 'student';
          }
          this.loading.set(false);
        },
        error: () => {
          this.requests.set([]);
          this.loading.set(false);
        },
      });
  }

  isSelected(id: string): boolean {
    return this.selectedIds().has(id);
  }

  toggle(id: string): void {
    const next = new Set(this.selectedIds());
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    this.selectedIds.set(next);
  }

  approve(r: RegistrationRequest): void {
    this.busy.set(true);
    this.http
      .post(`${environment.apiUrl}/registration/requests/${r.id}/approve`, {
        role: this.roleChoice[r.id] ?? 'student',
      })
      .subscribe({
        next: () => {
          this.snack.open(`${r.display_name} approved`, 'Dismiss', { duration: 3000 });
          this.busy.set(false);
          this.load();
        },
        error: (err) => {
          this.busy.set(false);
          this.snack.open(err?.error?.detail ?? 'Approval failed', 'Dismiss', { duration: 5000 });
        },
      });
  }

  reject(r: RegistrationRequest): void {
    const reason = window.prompt(`Why is ${r.display_name}'s request being declined?`);
    if (!reason) {
      return;
    }
    this.busy.set(true);
    this.http
      .post(`${environment.apiUrl}/registration/requests/${r.id}/reject`, { reason })
      .subscribe({
        next: () => {
          this.busy.set(false);
          this.load();
        },
        error: (err) => {
          this.busy.set(false);
          this.snack.open(err?.error?.detail ?? 'Could not decline', 'Dismiss', { duration: 5000 });
        },
      });
  }

  bulkApprove(): void {
    this.busy.set(true);
    this.http
      .post<{ approved: number; failed: number; results: { error: string | null }[] }>(
        `${environment.apiUrl}/registration/requests/bulk-approve`,
        { request_ids: this.selected(), role: this.bulkRole },
      )
      .subscribe({
        next: (res) => {
          this.busy.set(false);
          const msg = res.failed
            ? `${res.approved} approved, ${res.failed} failed`
            : `${res.approved} approved`;
          this.snack.open(msg, 'Dismiss', { duration: 5000 });
          this.load();
        },
        error: () => {
          this.busy.set(false);
          this.snack.open('Bulk approval failed', 'Dismiss', { duration: 5000 });
        },
      });
  }
}
