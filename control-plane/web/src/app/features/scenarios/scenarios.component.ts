import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatTooltipModule } from '@angular/material/tooltip';
import { FormsModule } from '@angular/forms';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Scenario } from '@core/models';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

@Component({
  selector: 'tn-scenarios',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatDialogModule, MatTableModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSlideToggleModule,
    MatTooltipModule, FormsModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">theaters</mat-icon>
          <div>
            <h1>Scenarios</h1>
            <p class="subtitle">Create and manage attack scenarios</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="startCreate()">
          <mat-icon>add</mat-icon> New Scenario
        </button>
      </div>

      <!-- CREATE / EDIT FORM -->
      @if (showForm) {
        <mat-card class="create-form mt-2">
          <mat-card-header>
            <mat-card-title>{{ editingId ? 'Edit Scenario' : 'New Scenario' }}</mat-card-title>
          </mat-card-header>
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline" class="flex-grow">
                <mat-label>Name</mat-label>
                <input matInput [(ngModel)]="form.name">
              </mat-form-field>
              <mat-form-field appearance="outline" class="version-field">
                <mat-label>Version</mat-label>
                <input matInput [(ngModel)]="form.version" placeholder="1.0">
              </mat-form-field>
            </div>
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>YAML Definition</mat-label>
              <textarea matInput class="yaml-input" [(ngModel)]="form.yaml" rows="14"></textarea>
            </mat-form-field>
            <mat-slide-toggle [(ngModel)]="form.is_public">Public</mat-slide-toggle>
            <div class="form-actions mt-1">
              @if (editingId) {
                <button mat-raised-button color="primary" (click)="update()" [disabled]="!form.name || saving">
                  <mat-icon>save</mat-icon> Save Changes
                </button>
              } @else {
                <button mat-raised-button color="primary" (click)="create()" [disabled]="!form.name || saving">
                  <mat-icon>add</mat-icon> Create
                </button>
              }
              <button mat-button (click)="cancelForm()">Cancel</button>
            </div>
          </mat-card-content>
        </mat-card>
      }

      <!-- SCENARIO TABLE -->
      <table mat-table [dataSource]="scenarios()" class="mt-2 full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let s">{{ s.name }}</td>
        </ng-container>
        <ng-container matColumnDef="version">
          <th mat-header-cell *matHeaderCellDef>Version</th>
          <td mat-cell *matCellDef="let s">{{ s.version }}</td>
        </ng-container>
        <ng-container matColumnDef="public">
          <th mat-header-cell *matHeaderCellDef>Public</th>
          <td mat-cell *matCellDef="let s">
            <mat-icon [class.text-green]="s.is_public">{{ s.is_public ? 'public' : 'lock' }}</mat-icon>
          </td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let s">{{ s.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let s">
            <button mat-icon-button matTooltip="Edit" (click)="startEdit(s)">
              <mat-icon>edit</mat-icon>
            </button>
            <button mat-icon-button matTooltip="Delete" color="warn" (click)="confirmDelete(s)">
              <mat-icon>delete</mat-icon>
            </button>
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="columns"></tr>
        <tr mat-row *matRowDef="let row; columns: columns"
            [class.selected-row]="row.id === editingId"></tr>
      </table>

      @if (loading()) {
        <div class="tn-skeleton-group mt-2" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
          <div class="tn-skeleton tn-skeleton-row"></div>
        </div>
      } @else if (scenarios().length === 0) {
        <tn-empty-state
          icon="theaters"
          title="No scenarios yet"
          message="Click New Scenario to create one."
        >
          <button mat-stroked-button (click)="startCreate()">
            <mat-icon>add</mat-icon> New Scenario
          </button>
        </tn-empty-state>
      }
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .full-width { width: 100%; }
    .form-row { display: flex; gap: 16px; align-items: flex-start; }
    .flex-grow { flex: 1; }
    .version-field { width: 160px; min-width: 160px; }
    .form-actions { display: flex; gap: 8px; }
    .text-green { color: var(--success); }
    .selected-row { background: var(--accent-muted); }
    .yaml-input { font-family: var(--font-mono); font-size: 13px; line-height: 1.5; }
  `],
})
export class ScenariosComponent implements OnInit {
  scenarios = signal<Scenario[]>([]);
  loading = signal(true);
  showForm = false;
  editingId: string | null = null;
  saving = false;
  form = { name: '', version: '1.0', yaml: '', is_public: false };
  columns = ['name', 'version', 'public', 'created', 'actions'];

  private readonly dialog = inject(MatDialog);

  constructor(private api: ApiService, private notify: NotificationService) {}
  ngOnInit(): void { this.load(); }

  load(): void {
    this.api.listScenarios().subscribe({
      next: s => { this.scenarios.set(s); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }

  // ── Create ────────────────────────────────────────────────
  startCreate(): void {
    this.editingId = null;
    this.form = { name: '', version: '1.0', yaml: '', is_public: false };
    this.showForm = true;
  }

  create(): void {
    this.saving = true;
    this.api.createScenario(this.form).subscribe({
      next: () => {
        this.notify.success('Scenario created');
        this.load();
        this.cancelForm();
      },
      error: () => { this.notify.error('Create failed'); this.saving = false; },
    });
  }

  // ── Edit ──────────────────────────────────────────────────
  startEdit(s: Scenario): void {
    this.editingId = s.id;
    this.form = { name: s.name, version: s.version, yaml: s.yaml, is_public: s.is_public };
    this.showForm = true;
  }

  update(): void {
    if (!this.editingId) return;
    this.saving = true;
    this.api.updateScenario(this.editingId, this.form).subscribe({
      next: () => {
        this.notify.success('Scenario updated');
        this.load();
        this.cancelForm();
      },
      error: () => { this.notify.error('Update failed'); this.saving = false; },
    });
  }

  // ── Delete ────────────────────────────────────────────────
  confirmDelete(s: Scenario): void {
    this.dialog
      .open(ConfirmDialogComponent, {
        data: {
          title: 'Delete Scenario',
          message: `Delete "${s.name}"? This action cannot be undone.`,
          confirmText: 'Delete',
        },
      })
      .afterClosed()
      .subscribe(ok => { if (ok) this.doDelete(s); });
  }

  private doDelete(s: Scenario): void {
    this.saving = true;
    this.api.deleteScenario(s.id).subscribe({
      next: () => {
        this.notify.success('Scenario deleted');
        this.saving = false;
        if (this.editingId === s.id) this.cancelForm();
        this.load();
      },
      error: () => { this.notify.error('Delete failed'); this.saving = false; },
    });
  }

  cancelForm(): void {
    this.showForm = false;
    this.editingId = null;
    this.saving = false;
    this.form = { name: '', version: '1.0', yaml: '', is_public: false };
  }
}
