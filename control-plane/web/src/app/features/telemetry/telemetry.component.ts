import { Component, OnDestroy, OnInit, effect, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTableModule } from '@angular/material/table';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatTooltipModule } from '@angular/material/tooltip';
import { NgxEchartsDirective, provideEcharts } from 'ngx-echarts';
import type { EChartsOption } from 'echarts';
import { Subscription, interval } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { Range, TelemetryEvent } from '@core/models';
import { ThemeService } from '@core/services/theme.service';
import { tnChartColors, tnCartesianBase } from '../../shared/charts/echarts-theme';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';

@Component({
  selector: 'tn-telemetry',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSelectModule, MatTableModule,
    MatSlideToggleModule, MatTooltipModule, NgxEchartsDirective, LottieIconComponent,
  ],
  // Component-level provider keeps echarts inside this route's lazy chunk.
  providers: [provideEcharts()],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">monitoring</mat-icon>
          <div>
            <div class="tn-kicker">Live Range Signal</div>
            <h1>Telemetry Explorer</h1>
            <p class="subtitle">Query and explore range telemetry events</p>
          </div>
        </div>
        <mat-slide-toggle
          [(ngModel)]="autoRefresh"
          matTooltip="Re-run the current search every 10 seconds (pauses while the tab is hidden)">
          Live refresh
        </mat-slide-toggle>
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
        <div class="charts-row">
          <mat-card class="chart-card chart-wide">
            <div class="chart-title">Event volume over time</div>
            <div echarts [options]="timelineOption()" class="chart chart-tall"></div>
          </mat-card>
          <mat-card class="chart-card">
            <div class="chart-title">Event types</div>
            <div echarts [options]="donutOption()" class="chart"></div>
          </mat-card>
          <mat-card class="chart-card">
            <div class="chart-title">Top hosts</div>
            <div echarts [options]="topHostsOption()" class="chart"></div>
          </mat-card>
        </div>

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
        <mat-card class="mt-2 empty-state">
          <tn-lottie name="radar-scan" [size]="120" />
          <p>No events matched this query. The range may be quiet — or the hunt continues.</p>
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .search-bar { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; }
    .search-bar mat-form-field { min-width: 200px; }
    .query-field { flex: 1; min-width: 300px; }
    .full-width { width: 100%; }
    .subtitle { color: var(--text-muted); }

    .charts-row {
      display: grid;
      grid-template-columns: 2fr 1fr 1fr;
      gap: 16px;
      margin-top: 18px;
    }
    @media (max-width: 1100px) { .charts-row { grid-template-columns: 1fr; } }
    .chart-card { padding: 14px 16px 8px; }
    .chart-title {
      font-size: 11px; font-weight: 700; letter-spacing: 1.4px;
      text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px;
    }
    .chart { height: 220px; width: 100%; }
    .chart-tall { height: 220px; }

    .empty-state {
      display: flex; flex-direction: column; align-items: center;
      padding: 40px; gap: 10px; color: var(--text-muted); text-align: center;
    }
  `],
})
export class TelemetryComponent implements OnInit, OnDestroy {
  ranges = signal<Range[]>([]);
  events = signal<TelemetryEvent[]>([]);
  timelineOption = signal<EChartsOption>({});
  donutOption = signal<EChartsOption>({});
  topHostsOption = signal<EChartsOption>({});

  selectedRangeId = '';
  query = '*';
  searched = false;
  autoRefresh = false;
  columns = ['timestamp', 'event_type', 'hostname', 'source_ip', 'process_name'];

  private refreshSub?: Subscription;

  constructor(private api: ApiService, private theme: ThemeService) {
    // Rebuild chart options whenever the theme accent changes.
    effect(() => {
      this.theme.activeTheme();
      if (this.events().length) {
        this.buildCharts(this.events());
      }
    });
  }

  ngOnInit(): void {
    this.api.listRanges().subscribe({ next: r => this.ranges.set(r), error: () => {} });
    this.refreshSub = interval(10_000).subscribe(() => {
      if (this.autoRefresh && this.selectedRangeId && document.visibilityState === 'visible') {
        this.search();
      }
    });
  }

  ngOnDestroy(): void {
    this.refreshSub?.unsubscribe();
  }

  search(): void {
    this.searched = true;
    this.api.searchTelemetry(this.selectedRangeId, this.query).subscribe({
      next: res => {
        const hits = (res.hits?.hits || []).map(h => h._source);
        this.events.set(hits);
        this.buildCharts(hits);
      },
      error: () => this.events.set([]),
    });
  }

  private buildCharts(events: TelemetryEvent[]): void {
    const c = tnChartColors();
    const base = tnCartesianBase(c);

    // Time buckets (per minute).
    const buckets = new Map<string, number>();
    for (const e of events) {
      const ts = e.timestamp ? String(e.timestamp).slice(0, 16) : 'unknown';
      buckets.set(ts, (buckets.get(ts) ?? 0) + 1);
    }
    const times = [...buckets.keys()].sort();
    this.timelineOption.set({
      ...base,
      xAxis: {
        type: 'category',
        data: times.map(t => t.slice(11) || t),
        axisLine: { lineStyle: { color: c.border } },
        axisLabel: { color: c.textMuted },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: c.border, opacity: 0.5 } },
        axisLabel: { color: c.textMuted },
      },
      series: [{
        type: 'bar',
        data: times.map(t => buckets.get(t)),
        itemStyle: { color: c.accent, borderRadius: [3, 3, 0, 0] },
        emphasis: { itemStyle: { color: c.accentHover } },
      }],
    } as EChartsOption);

    // Event type donut.
    const types = new Map<string, number>();
    for (const e of events) {
      const t = e.event_type || 'unknown';
      types.set(t, (types.get(t) ?? 0) + 1);
    }
    this.donutOption.set({
      color: c.palette,
      textStyle: { color: c.textMuted, fontFamily: 'Inter, sans-serif' },
      tooltip: {
        trigger: 'item',
        backgroundColor: c.card,
        borderColor: c.border,
        textStyle: { color: c.text },
      },
      series: [{
        type: 'pie',
        radius: ['55%', '80%'],
        itemStyle: { borderColor: c.card, borderWidth: 2 },
        label: { color: c.textMuted, fontSize: 11 },
        data: [...types.entries()].map(([name, value]) => ({ name, value })),
      }],
    } as EChartsOption);

    // Top hosts horizontal bar.
    const hosts = new Map<string, number>();
    for (const e of events) {
      const h = e.hostname || 'unknown';
      hosts.set(h, (hosts.get(h) ?? 0) + 1);
    }
    const top = [...hosts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).reverse();
    this.topHostsOption.set({
      ...base,
      xAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: c.border, opacity: 0.5 } },
        axisLabel: { color: c.textMuted },
      },
      yAxis: {
        type: 'category',
        data: top.map(([name]) => name),
        axisLine: { lineStyle: { color: c.border } },
        axisLabel: { color: c.textMuted },
      },
      series: [{
        type: 'bar',
        data: top.map(([, count]) => count),
        itemStyle: { color: c.accent, borderRadius: [0, 3, 3, 0] },
        emphasis: { itemStyle: { color: c.accentHover } },
      }],
    } as EChartsOption);
  }
}
