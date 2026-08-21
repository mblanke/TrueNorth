import {
  Component, ElementRef, ViewChild, AfterViewInit, OnDestroy, Input, effect, inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { MatCardModule } from '@angular/material/card';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { FormsModule } from '@angular/forms';
import { ThemeService } from '@core/services/theme.service';
import { tnChartColors } from '../../shared/charts/echarts-theme';

/**
 * NICE Framework competency categories (SP 800-181r1)
 * Used as Y-axis for the heatmap
 */
const NICE_CATEGORIES = [
  'Analyze', 'Collect & Operate', 'Investigate', 'Operate & Maintain',
  'Oversee & Govern', 'Protect & Defend', 'Securely Provision',
];

const PROFICIENCY_LEVELS = ['Novice', 'Beginner', 'Intermediate', 'Advanced', 'Expert'];

interface HeatmapData {
  categories: string[];
  work_roles: string[];
  values: number[][]; // [categoryIdx, roleIdx, score 0-100]
}

@Component({
  selector: 'tn-competency-heatmap',
  standalone: true,
  imports: [CommonModule, MatCardModule, MatSelectModule, MatFormFieldModule, FormsModule],
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
        <div #chartContainer class="chart-container"></div>
      </mat-card-content>
    </mat-card>
  `,
  styles: [`
    :host { display: block; }
    .chart-container { width: 100%; height: 500px; }
    .heatmap-controls { display: flex; gap: 16px; margin-bottom: 8px; }
  `],
})
export class CompetencyHeatmapComponent implements AfterViewInit, OnDestroy {
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLDivElement>;
  @Input() tenantId?: string;

  viewMode = 'team';
  private chartInstance: any = null;
  private lastData: HeatmapData | null = null;
  private readonly theme = inject(ThemeService);

  constructor(private http: HttpClient) {
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
    this.chartInstance = echarts.init(this.chartContainer.nativeElement);
    this.renderChart();

    // Handle resize
    const observer = new ResizeObserver(() => this.chartInstance?.resize());
    observer.observe(this.chartContainer.nativeElement);
  }

  ngOnDestroy() {
    this.chartInstance?.dispose();
  }

  renderChart() {
    if (!this.chartInstance) return;

    // Fetch data from API or use sample data
    this.http.get<HeatmapData>('/api/competency/heatmap', {
      params: { view: this.viewMode },
    }).subscribe({
      next: data => this.updateChart(data),
      error: () => this.updateChart(this.sampleData()),
    });
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

  private sampleData(): HeatmapData {
    const roles = [
      'SOC Analyst', 'Incident Resp.', 'Threat Hunter',
      'Vuln Analyst', 'Pen Tester', 'Forensic Analyst',
    ];
    const values: number[][] = [];
    for (let x = 0; x < roles.length; x++) {
      for (let y = 0; y < NICE_CATEGORIES.length; y++) {
        values.push([x, y, Math.floor(Math.random() * 80 + 10)]);
      }
    }
    return { categories: NICE_CATEGORIES, work_roles: roles, values };
  }
}
