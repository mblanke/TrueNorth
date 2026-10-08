import {
  Component, ElementRef, ViewChild, AfterViewInit, OnDestroy, Input, effect, inject, signal,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { ApiService, CompetencyHeatmap } from '@core/services/api.service';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { FormsModule } from '@angular/forms';
import { ThemeService } from '@core/services/theme.service';
import { tnChartColors } from '../../shared/charts/echarts-theme';

const PROFICIENCY_LEVELS = ['Novice', 'Beginner', 'Intermediate', 'Advanced', 'Expert'];

type HeatmapData = CompetencyHeatmap;

@Component({
  selector: 'tn-competency-heatmap',
  imports: [MatButtonModule, MatCardModule, MatSelectModule, MatFormFieldModule, FormsModule],
  template: `
    <mat-card>
      <mat-card-header>
        <mat-card-title>Competency Heatmap</mat-card-title>
        <mat-card-subtitle>
          NICE Framework proficiency across work roles.
          Darker cells indicate higher proficiency.
        </mat-card-subtitle>
      </mat-card-header>
      <mat-card-content>
        <div class="heatmap-controls">
          <mat-form-field appearance="outline">
            <mat-label>View</mat-label>
            <mat-select [(ngModel)]="viewMode" (selectionChange)="renderChart()">
              <mat-option value="team">Team Average</mat-option>
              <mat-option value="individual">Individual</mat-option>
            </mat-select>
          </mat-form-field>
        </div>
        @switch (status()) {
          @case ('loading') {
            <p class="heatmap-state" role="status">Loading competency data…</p>
          }
          @case ('error') {
            <div class="heatmap-state heatmap-error" role="alert">
              <p>Competency data could not be loaded.</p>
              <button mat-stroked-button type="button" (click)="renderChart()">Retry</button>
            </div>
          }
          @case ('empty') {
            <p class="heatmap-state heatmap-empty" role="status">
              No competency assessments recorded yet.
            </p>
          }
        }
        <!-- Kept in the DOM so ECharts has a stable host; hidden unless real data is plotted. -->
        <div #chartContainer class="chart-container" [class.chart-hidden]="status() !== 'ready'"
             [attr.aria-hidden]="status() !== 'ready'"></div>
      </mat-card-content>
    </mat-card>
  `,
  styles: [`
    :host { display: block; }
    .chart-container { width: 100%; height: 500px; }
    .chart-container.chart-hidden { visibility: hidden; height: 0; }
    .heatmap-controls { display: flex; gap: 16px; margin-bottom: 8px; }
    .heatmap-state { padding: 32px 0; text-align: center; color: var(--text-muted); }
    .heatmap-error p { margin: 0 0 12px; }
  `],
})
export class CompetencyHeatmapComponent implements AfterViewInit, OnDestroy {
  private api = inject(ApiService);

  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;
  @Input() tenantId?: string;

  viewMode = 'team';
  /** What the card is showing. Only 'ready' plots numbers, and only ones the API returned. */
  readonly status = signal<'loading' | 'ready' | 'empty' | 'error'>('loading');
  private resizeObserver?: ResizeObserver;
  private dataSub?: Subscription;
  private destroyed = false;
  private chartInstance: any = null;
  private lastData: HeatmapData | null = null;
  private readonly theme = inject(ThemeService);

  constructor() {
    // ECharts snapshots CSS variables at option-build time, so a theme switch
    // must rebuild the option or the old palette sticks.
    effect(() => {
      this.theme.activeTheme();
      if (this.chartInstance && this.lastData) this.updateChart(this.lastData);
    });
  }

  async ngAfterViewInit() {
    // Dynamic import to avoid loading ECharts in the initial bundle
    const echarts = await import('echarts');
    // The component can be destroyed while ECharts loads.
    if (this.destroyed) return;
    this.chartInstance = echarts.init(this.chartContainer.nativeElement);
    this.renderChart();

    // Handle resize
    this.resizeObserver = new ResizeObserver(() => this.chartInstance?.resize());
    this.resizeObserver.observe(this.chartContainer.nativeElement);
  }

  ngOnDestroy() {
    this.destroyed = true;
    this.dataSub?.unsubscribe();
    this.resizeObserver?.disconnect();
    this.resizeObserver = undefined;
    this.chartInstance?.dispose();
    this.chartInstance = null;
  }

  renderChart() {
    if (!this.chartInstance) return;

    this.dataSub?.unsubscribe();
    this.status.set('loading');
    // A failed request is an error, never plausible numbers: no sample-data fallback.
    this.dataSub = this.api.getCompetencyHeatmap(this.viewMode).subscribe({
      next: data => {
        if (!data?.values?.length || !data.work_roles?.length || !data.categories?.length) {
          this.clearChart();
          this.status.set('empty');
          return;
        }
        this.updateChart(data);
        this.status.set('ready');
        // The container was hidden at height 0 while loading; size the chart to it now.
        queueMicrotask(() => this.chartInstance?.resize());
      },
      error: () => {
        this.clearChart();
        this.status.set('error');
      },
    });
  }

  private clearChart() {
    this.lastData = null;
    this.chartInstance?.clear();
  }

  private updateChart(data: HeatmapData) {
    this.lastData = data;
    const c = tnChartColors();
    const option = {
      tooltip: {
        position: 'top',
        backgroundColor: c.card,
        borderColor: c.border,
        textStyle: { color: c.text },
        formatter: (params: any) => {
          const cat = data.categories[params.value[1]];
          const role = data.work_roles[params.value[0]];
          const score = params.value[2];
          const level = PROFICIENCY_LEVELS[Math.min(Math.floor(score / 20), 4)];
          return `<b>${role}</b><br/>${cat}<br/>Score: ${score}% (${level})`;
        },
      },
      grid: {
        top: 30, bottom: 100, left: 180, right: 40,
      },
      xAxis: {
        type: 'category',
        data: data.work_roles,
        splitArea: { show: true },
        axisLabel: { rotate: 45, fontSize: 11, color: c.textMuted },
        axisLine: { lineStyle: { color: c.border } },
      },
      yAxis: {
        type: 'category',
        data: data.categories,
        splitArea: { show: true },
        axisLabel: { color: c.textMuted },
        axisLine: { lineStyle: { color: c.border } },
      },
      visualMap: {
        min: 0,
        max: 100,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        // Low-to-high ramp built from the live theme, so it reads on all four
        // (the old hardcoded purple/pink ramp matched none of them).
        inRange: {
          color: [c.card, c.border, c.textMuted, c.warning, c.accent],
        },
        text: ['Expert', 'Novice'],
        textStyle: { color: c.textMuted },
      },
      series: [{
        type: 'heatmap',
        data: data.values,
        label: {
          show: true,
          formatter: (p: any) => `${p.value[2]}`,
          fontSize: 10,
          color: c.text,
        },
        emphasis: {
          itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0, 0, 0, 0.35)' },
        },
      }],
    };

    this.chartInstance.setOption(option, true);
  }
}
