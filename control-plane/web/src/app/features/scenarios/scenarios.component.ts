import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatTableModule } from '@angular/material/table';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { FormsModule } from '@angular/forms';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Scenario } from '@core/models';

@Component({
  selector: 'tn-scenarios',
  standalone: true,
  imports: [
    CommonModule, MatCardModule, MatTableModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSlideToggleModule, FormsModule,
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
        <button mat-raised-button color="primary" (click)="showCreate = !showCreate">
          <mat-icon>add</mat-icon> New Scenario
        </button>
      </div>

      @if (showCreate) {
        <mat-card class="mt-2">
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
              <textarea matInput [(ngModel)]="form.yaml" rows="12" style="font-family: monospace;"></textarea>
            </mat-form-field>
            <mat-slide-toggle [(ngModel)]="form.is_public">Public</mat-slide-toggle>
            <div class="mt-1">
              <button mat-raised-button color="primary" (click)="create()">Create</button>
              <button mat-button (click)="showCreate = false">Cancel</button>
            </div>
          </mat-card-content>
        </mat-card>
      }

      <table mat-table [dataSource]="scenarios()" class="mt-2 full-width">
        <ng-container matColumnDef="name"><th mat-header-cell *matHeaderCellDef>Name</th><td mat-cell *matCellDef="let s">{{ s.name }}</td></ng-container>
        <ng-container matColumnDef="version"><th mat-header-cell *matHeaderCellDef>Version</th><td mat-cell *matCellDef="let s">{{ s.version }}</td></ng-container>
        <ng-container matColumnDef="public"><th mat-header-cell *matHeaderCellDef>Public</th><td mat-cell *matCellDef="let s">{{ s.is_public ? 'Yes' : 'No' }}</td></ng-container>
        <ng-container matColumnDef="created"><th mat-header-cell *matHeaderCellDef>Created</th><td mat-cell *matCellDef="let s">{{ s.created_at | date:'short' }}</td></ng-container>
        <tr mat-header-row *matHeaderRowDef="columns"></tr>
        <tr mat-row *matRowDef="let row; columns: columns"></tr>
      </table>
    </div>
  `,
  styles: [`.page-header { display: flex; justify-content: space-between; align-items: center; }
    .full-width { width: 100%; }
    mat-card-content { display: flex; flex-direction: column; gap: 12px; }
    .form-row { display: flex; gap: 16px; align-items: flex-start; }
    .flex-grow { flex: 1; }
    .version-field { width: 160px; min-width: 160px; }`],
})
export class ScenariosComponent implements OnInit {
  scenarios = signal<Scenario[]>([]);
  showCreate = false;
  form = { name: '', version: '1.0', yaml: '', is_public: false };
  columns = ['name', 'version', 'public', 'created'];

  constructor(private api: ApiService, private notify: NotificationService) {}
  ngOnInit(): void { this.load(); }
  load(): void { this.api.listScenarios().subscribe(s => this.scenarios.set(s)); }
  create(): void {
    this.api.createScenario(this.form).subscribe({
      next: () => { this.notify.success('Scenario created'); this.load(); this.showCreate = false; },
      error: () => this.notify.error('Failed'),
    });
  }
}
