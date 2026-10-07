import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';

import { ApiService, CompetencyHeatmap } from '@core/services/api.service';
import { ThemeService } from '@core/services/theme.service';
import { CompetencyHeatmapComponent } from './competency-heatmap.component';

/**
 * The component dynamically imports ECharts in ngAfterViewInit and only then
 * fetches data, so the tests wait for the API call rather than for zone stability.
 */
describe('CompetencyHeatmapComponent', () => {
  let fixture: ComponentFixture<CompetencyHeatmapComponent>;
  let component: CompetencyHeatmapComponent;
  let api: jasmine.SpyObj<ApiService>;

  const DATA: CompetencyHeatmap = {
    categories: ['Analyze', 'Investigate'],
    work_roles: ['SOC Analyst', 'Threat Hunter'],
    values: [[0, 0, 40], [0, 1, 55], [1, 0, 80], [1, 1, 20]],
  };

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['getCompetencyHeatmap']);
    await TestBed.configureTestingModule({
      imports: [CompetencyHeatmapComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: ThemeService, useValue: { activeTheme: signal('dark') } },
      ],
    }).compileComponents();
  });

  afterEach(() => fixture?.destroy());

  async function untilCalled(spy: jasmine.Spy, times = 1): Promise<void> {
    for (let i = 0; i < 200 && spy.calls.count() < times; i++) {
      await new Promise(r => setTimeout(r, 10));
    }
    expect(spy.calls.count()).toBeGreaterThanOrEqual(times);
  }

  function chartOption(): any {
    return (component as any).chartInstance.getOption();
  }

  async function create(): Promise<void> {
    fixture = TestBed.createComponent(CompetencyHeatmapComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await untilCalled(api.getCompetencyHeatmap);
  }

  it('creates, requests the team view and plots the returned roles and categories', async () => {
    api.getCompetencyHeatmap.and.returnValue(of(DATA));
    await create();
    expect(component).toBeTruthy();
    expect(api.getCompetencyHeatmap).toHaveBeenCalledWith('team');
    const opt = chartOption();
    expect(opt.xAxis[0].data).toEqual(DATA.work_roles);
    expect(opt.yAxis[0].data).toEqual(DATA.categories);
    expect(opt.series[0].data.length).toBe(4);
  });

  it('re-requests with the individual view when the view changes', async () => {
    api.getCompetencyHeatmap.and.returnValue(of(DATA));
    await create();
    component.viewMode = 'individual';
    component.renderChart();
    expect(api.getCompetencyHeatmap).toHaveBeenCalledWith('individual');
  });

  it('falls back to sample data instead of crashing when the API fails', async () => {
    api.getCompetencyHeatmap.and.returnValue(throwError(() => ({ status: 500 })));
    await create();
    const opt = chartOption();
    expect(opt.xAxis[0].data).toContain('SOC Analyst');
    expect(opt.yAxis[0].data.length).toBe(7);
  });

  it('does nothing before the chart is initialised', () => {
    fixture = TestBed.createComponent(CompetencyHeatmapComponent);
    fixture.componentInstance.renderChart();
    expect(api.getCompetencyHeatmap).not.toHaveBeenCalled();
  });
});
