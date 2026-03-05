import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { FormsModule } from '@angular/forms';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Range, Template } from '@core/models';

@Component({
  selector: 'tn-ranges',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatTableModule, MatButtonModule,
    MatIconModule, MatChipsModule, MatDialogModule, MatFormFieldModule,
    MatInputModule, MatSelectModule, MatTooltipModule, FormsModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">cloud</mat-icon>
          <div>
            <h1>Ranges</h1>
            <p class="subtitle">Deploy and manage cyber ranges</p>
          </div>
        </div>
        <button mat-raised-button color="primary" (click)="showCreate = !showCreate">
          <mat-icon>add</mat-icon> New Range
        </button>
      </div>

      @if (showCreate) {
        <mat-card class="create-form mt-2">
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Range Name</mat-label>
                <input matInput [(ngModel)]="newName" placeholder="Training Range 01">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Template</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedTemplateId">
                @for (t of templates(); track t.id) {
                  <mat-option [value]="t.id">{{ t.name }} ({{ t.version }})</mat-option>
                }
              </mat-select>
              </mat-form-field>
            </div>
            <button mat-raised-button color="primary" (click)="createRange()" [disabled]="!newName || !selectedTemplateId">
              Create
            </button>
            <button mat-button (click)="showCreate = false">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      @if (editingId) {
        <mat-card class="edit-form mt-2">
          <mat-card-content>
            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Range Name</mat-label>
                <input matInput [(ngModel)]="editForm.name" placeholder="Range name">
              </mat-form-field>
            </div>
            <button mat-raised-button color="primary" (click)="updateRange()" [disabled]="!editForm.name || saving">
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
            <button mat-button (click)="cancelEdit()" [disabled]="saving">Cancel</button>
          </mat-card-content>
        </mat-card>
      }

      <table mat-table [dataSource]="ranges()" class="mt-2 full-width">
        <ng-container matColumnDef="name">
          <th mat-header-cell *matHeaderCellDef>Name</th>
          <td mat-cell *matCellDef="let r">{{ r.name }}</td>
        </ng-container>
        <ng-container matColumnDef="state">
          <th mat-header-cell *matHeaderCellDef>State</th>
          <td mat-cell *matCellDef="let r">
            <span class="status-chip" [class]="r.state">{{ r.state }}</span>
          </td>
        </ng-container>
        <ng-container matColumnDef="created">
          <th mat-header-cell *matHeaderCellDef>Created</th>
          <td mat-cell *matCellDef="let r">{{ r.created_at | date:'short' }}</td>
        </ng-container>
        <ng-container matColumnDef="actions">
          <th mat-header-cell *matHeaderCellDef>Actions</th>
          <td mat-cell *matCellDef="let r">
            @if (r.state === 'created' || r.state === 'stopped') {
              <button mat-icon-button (click)="startEdit(r)" matTooltip="Rename" [disabled]="saving">
                <mat-icon>edit</mat-icon>
              </button>
            }
            @if (r.state === 'created') {
              <button mat-icon-button color="primary" (click)="provision(r.id)" matTooltip="Provision">
                <mat-icon>rocket_launch</mat-icon>
              </button>
            }
            @if (r.state === 'ready' || r.state === 'running') {
              <button mat-icon-button (click)="stop(r.id)" matTooltip="Stop">
                <mat-icon>stop</mat-icon>
              </button>
            }
            @if (r.state === 'ready' || r.state === 'running' || r.state === 'stopped') {
              <button mat-icon-button color="warn" (click)="destroy(r.id)" matTooltip="Destroy">
                <mat-icon>delete</mat-icon>
              </button>
            }
            @if (r.error_message) {
              <button mat-icon-button matTooltip="{{ r.error_message }}">
                <mat-icon color="warn">error</mat-icon>
              </button>
            }
          </td>
        </ng-container>
        <tr mat-header-row *matHeaderRowDef="displayedColumns"></tr>
        <tr mat-row *matRowDef="let row; columns: displayedColumns"></tr>
      </table>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .full-width { width: 100%; }
    .create-form mat-card-content, .edit-form mat-card-content { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
    .create-form mat-form-field, .edit-form mat-form-field { flex: 1; min-width: 200px; }
  `],
})
export class RangesComponent implements OnInit {
  ranges = signal<Range[]>([]);
  templates = signal<Template[]>([]);
  showCreate = false;
  newName = '';
  selectedTemplateId = '';
  editingId = '';
  editForm = { name: '' };
  saving = false;
  displayedColumns = ['name', 'state', 'created', 'actions'];

  constructor(private api: ApiService, private notify: NotificationService) {}

  ngOnInit(): void {
    this.loadRanges();
    this.api.listTemplates().subscribe(t => this.templates.set(t));
  }

  loadRanges(): void {
    this.api.listRanges().subscribe(r => this.ranges.set(r));
  }

  createRange(): void {
    this.api.createRange({ name: this.newName, template_id: this.selectedTemplateId }).subscribe({
      next: () => { this.notify.success('Range created'); this.loadRanges(); this.showCreate = false; this.newName = ''; },
      error: () => this.notify.error('Failed to create range'),
    });
  }

  startEdit(r: Range): void {
    this.editingId = r.id;
    this.editForm.name = r.name;
  }

  updateRange(): void {
    this.saving = true;
    this.api.updateRange(this.editingId, this.editForm).subscribe({
      next: () => { this.notify.success('Range renamed'); this.loadRanges(); this.cancelEdit(); this.saving = false; },
      error: () => { this.notify.error('Failed to rename range'); this.saving = false; },
    });
  }

  cancelEdit(): void {
    this.editingId = '';
    this.editForm.name = '';
  }

  provision(id: string): void {
    this.api.provisionRange(id).subscribe({
      next: () => { this.notify.success('Provisioning started'); this.loadRanges(); },
      error: () => this.notify.error('Provisioning failed'),
    });
  }

  stop(id: string): void {
    this.api.stopRange(id).subscribe({
      next: () => { this.notify.success('Range stopped'); this.loadRanges(); },
      error: () => this.notify.error('Stop failed'),
    });
  }

  destroy(id: string): void {
    this.api.destroyRange(id).subscribe({
      next: () => { this.notify.success('Destroying range'); this.loadRanges(); },
      error: () => this.notify.error('Destroy failed'),
    });
  }
}
