import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTableModule } from '@angular/material/table';
import { ApiService } from '@core/services/api.service';
import { Range, TelemetryEvent } from '@core/models';

@Component({
  selector: 'tn-telemetry',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSelectModule, MatTableModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">monitoring</mat-icon>
          <div>
            <h1>Telemetry Explorer</h1>
            <p class="subtitle">Query and explore range telemetry events</p>
          </div>
        </div>
      </div>

      <div class="search-bar">
        <mat-form-field appearance="outline">
          <mat-label>Range</mat-label>
          <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedRangeId">
            @for (r of ranges(); track r.id) {
              <mat-option [value]="r.id">{{ r.name }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline" class="query-field">
          <mat-label>Query</mat-label>
          <input matInput [(ngModel)]="query" placeholder="event_type:process_create AND process_name:powershell*">
        </mat-form-field>
        <button mat-raised-button color="primary" (click)="search()" [disabled]="!selectedRangeId">
          <mat-icon>search</mat-icon> Search
        </button>
      </div>

      @if (events().length > 0) {
        <table mat-table [dataSource]="events()" class="mt-2 full-width">
          <ng-container matColumnDef="timestamp"><th mat-header-cell *matHeaderCellDef>Time</th><td mat-cell *matCellDef="let e">{{ e.timestamp }}</td></ng-container>
          <ng-container matColumnDef="event_type"><th mat-header-cell *matHeaderCellDef>Type</th><td mat-cell *matCellDef="let e">{{ e.event_type }}</td></ng-container>
          <ng-container matColumnDef="hostname"><th mat-header-cell *matHeaderCellDef>Host</th><td mat-cell *matCellDef="let e">{{ e.hostname || '—' }}</td></ng-container>
          <ng-container matColumnDef="source_ip"><th mat-header-cell *matHeaderCellDef>Src IP</th><td mat-cell *matCellDef="let e">{{ e.source_ip || '—' }}</td></ng-container>
          <ng-container matColumnDef="process_name"><th mat-header-cell *matHeaderCellDef>Process</th><td mat-cell *matCellDef="let e">{{ e.process_name || '—' }}</td></ng-container>
          <tr mat-header-row *matHeaderRowDef="columns"></tr>
          <tr mat-row *matRowDef="let row; columns: columns"></tr>
        </table>
      } @else if (searched) {
        <mat-card class="mt-2"><mat-card-content>No events found.</mat-card-content></mat-card>
      }
    </div>
  `,
  styles: [`
    .search-bar { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
    .search-bar mat-form-field { min-width: 200px; }
    .query-field { flex: 1; min-width: 300px; }
    .full-width { width: 100%; }
  `],
})
export class TelemetryComponent implements OnInit {
  ranges = signal<Range[]>([]);
  events = signal<TelemetryEvent[]>([]);
  selectedRangeId = '';
  query = '*';
  searched = false;
  columns = ['timestamp', 'event_type', 'hostname', 'source_ip', 'process_name'];

  constructor(private api: ApiService) {}
  ngOnInit(): void { this.api.listRanges().subscribe(r => this.ranges.set(r)); }

  search(): void {
    this.searched = true;
    this.api.searchTelemetry(this.selectedRangeId, this.query).subscribe({
      next: res => this.events.set((res.hits?.hits || []).map(h => h._source)),
      error: () => this.events.set([]),
    });
  }
}
