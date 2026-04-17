import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
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
import { Template } from '@core/models';

@Component({
  selector: 'tn-templates',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatTableModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSlideToggleModule,
    MatTooltipModule, FormsModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">description</mat-icon>
          <div>
            <h1>Range Templates</h1>
            <p class="subtitle">Create and manage range templates</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="startCreate()">
          <mat-icon>add</mat-icon> New Template
        </button>
      </div>

      <!-- CREATE / EDIT FORM -->
      @if (showForm) {
        <mat-card class="create-form mt-2">
          <mat-card-header>
            <mat-card-title>{{ editingId ? 'Edit Template' : 'New Template' }}</mat-card-title>
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
              <textarea matInput [(ngModel)]="form.yaml" rows="14"
                        style="font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px; line-height: 1.5;"></textarea>
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

      <!-- TEMPLATE TABLE -->
      <table mat-table [dataSource]="templates()" class="mt-2 full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let t">{{ t.name }}</td>
        </ng-container>
        <ng-container matColumnDef="version">
          <th mat-header-cell *matHeaderCellDef>Version</th>
          <td mat-cell *matCellDef="let t">{{ t.version }}</td>
        </ng-container>
        <ng-container matColumnDef="public">
          <th mat-header-cell *matHeaderCellDef>Public</th>
          <td mat-cell *matCellDef="let t">
            <mat-icon [class.text-green]="t.is_public">{{ t.is_public ? 'public' : 'lock' }}</mat-icon>
          </td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let t">{{ t.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let t">
            <button mat-icon-button matTooltip="Edit" (click)="startEdit(t)">
              <mat-icon>edit</mat-icon>
            </button>
            <button mat-icon-button matTooltip="Delete" color="warn" (click)="confirmDelete(t)">
              <mat-icon>delete</mat-icon>
            </button>
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="columns"></tr>
        <tr mat-row *matRowDef="let row; columns: columns"
            [class.selected-row]="row.id === editingId"></tr>
      </table>

      @if (templates().length === 0) {
        <div class="empty-state mt-2">
          <mat-icon>description</mat-icon>
          <p>No templates yet. Click <strong>New Template</strong> to create one.</p>
        </div>
      }

      <!-- DELETE CONFIRMATION -->
      @if (deleteTarget) {
        <div class="confirm-overlay" (click)="deleteTarget = null" (keyup.escape)="deleteTarget = null" tabindex="0" role="dialog">
          <mat-card class="confirm-dialog" (click)="$event.stopPropagation()">
            <mat-card-header>
              <mat-card-title>Delete Template</mat-card-title>
            </mat-card-header>
            <mat-card-content>
              <p>Are you sure you want to delete <strong>{{ deleteTarget.name }}</strong>?</p>
              <p class="warn-text">This action cannot be undone.</p>
            </mat-card-content>
            <mat-card-actions align="end">
              <button mat-button (click)="deleteTarget = null">Cancel</button>
              <button mat-raised-button color="warn" (click)="doDelete()" [disabled]="saving">
                <mat-icon>delete</mat-icon> Delete
              </button>
            </mat-card-actions>
          </mat-card>
        </div>
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
    .text-green { color: #4caf50; }
    .selected-row { background: rgba(0, 188, 212, 0.08); }
    .empty-state {
      display: flex; flex-direction: column; align-items: center;
      padding: 48px 16px; opacity: 0.6;
      mat-icon { font-size: 48px; width: 48px; height: 48px; }
    }
    .confirm-overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,0.5);
      display: flex; align-items: center; justify-content: center; z-index: 1000;
    }
    .confirm-dialog { max-width: 420px; width: 100%; }
    .warn-text { color: #ef5350; font-size: 0.85em; }
  `],
})
export class TemplatesComponent implements OnInit {
  templates = signal<Template[]>([]);
  showForm = false;
  editingId: string | null = null;
  deleteTarget: Template | null = null;
  saving = false;
  form = { name: '', version: '1.0', yaml: '', is_public: false };
  columns = ['name', 'version', 'public', 'created', 'actions'];

  constructor(private api: ApiService, private notify: NotificationService) {}
  ngOnInit(): void { this.load(); }

  load(): void { this.api.listTemplates().subscribe(t => this.templates.set(t)); }

  // ── Create ────────────────────────────────────────────────
  startCreate(): void {
    this.editingId = null;
    this.form = { name: '', version: '1.0', yaml: '', is_public: false };
    this.showForm = true;
  }

  create(): void {
    this.saving = true;
    this.api.createTemplate(this.form).subscribe({
      next: () => {
        this.notify.success('Template created');
        this.load();
        this.cancelForm();
      },
      error: () => { this.notify.error('Create failed'); this.saving = false; },
    });
  }

  // ── Edit ──────────────────────────────────────────────────
  startEdit(t: Template): void {
    this.editingId = t.id;
    this.form = { name: t.name, version: t.version, yaml: t.yaml, is_public: t.is_public };
    this.showForm = true;
  }

  update(): void {
    if (!this.editingId) return;
    this.saving = true;
    this.api.updateTemplate(this.editingId, this.form).subscribe({
      next: () => {
        this.notify.success('Template updated');
        this.load();
        this.cancelForm();
      },
      error: () => { this.notify.error('Update failed'); this.saving = false; },
    });
  }

  // ── Delete ────────────────────────────────────────────────
  confirmDelete(t: Template): void {
    this.deleteTarget = t;
  }

  doDelete(): void {
    if (!this.deleteTarget) return;
    const deletedId = this.deleteTarget.id;
    this.saving = true;
    this.api.deleteTemplate(deletedId).subscribe({
      next: () => {
        this.notify.success('Template deleted');
        this.deleteTarget = null;
        this.saving = false;
        if (this.editingId === deletedId) this.cancelForm();
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