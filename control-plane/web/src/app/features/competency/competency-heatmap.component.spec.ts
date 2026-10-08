import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Observable, of, throwError } from 'rxjs';

import { ApiService, CompetencyHeatmap } from '@core/services/api.service';
import { ThemeService } from '@core/services/theme.service';
import { CompetencyHeatmapComponent } from './competency-heatmap.component';

/**
 * The component dynamically imports ECharts in ngAfterViewInit and only then
 * fetches data. The tests await that very ngAfterViewInit promise: no polling and
 * no wall-clock cap. (They used to poll for the API call for at most 2 s, which a
 * cold ECharts chunk load on a busy CI runner could outlast.) The chunk is loaded
 * once up front, so no single test pays for it against its own timeout.
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

  beforeAll(async () => {
    await import('echarts');
  }, 60_000);

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

  function chartOption(): any {
    return (component as any).chartInstance.getOption();
  }

  /**
   * Render with the API answering `reply`, and resume once the component has asked for
   * the data (ECharts loaded, chart created). An event, not a poll: the request itself
   * resolves the wait, and ngAfterViewInit has finished by the time this returns.
   */
  async function create(reply: Observable<CompetencyHeatmap>): Promise<void> {
    let requested!: () => void;
    const request = new Promise<void>(resolve => (requested = resolve));
    api.getCompetencyHeatmap.and.callFake(() => {
      requested();
      return reply;
    });
    fixture = TestBed.createComponent(CompetencyHeatmapComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await request;
    expect(api.getCompetencyHeatmap).toHaveBeenCalledTimes(1);
  }

  it('creates, requests the team view and plots the returned roles and categories', async () => {
    await create(of(DATA));
    expect(component).toBeTruthy();
    expect(api.getCompetencyHeatmap).toHaveBeenCalledWith('team');
    const opt = chartOption();
    expect(opt.xAxis[0].data).toEqual(DATA.work_roles);
    expect(opt.yAxis[0].data).toEqual(DATA.categories);
    expect(opt.series[0].data.length).toBe(4);
  });

  it('re-requests with the individual view when the view changes', async () => {
    await create(of(DATA));
    component.viewMode = 'individual';
    component.renderChart();
    expect(api.getCompetencyHeatmap).toHaveBeenCalledWith('individual');
  });

  it('shows an error state, not invented numbers, when the API fails', async () => {
    await create(throwError(() => ({ status: 500 })));
    fixture.detectChanges();
    expect(component.status()).toBe('error');
    // Nothing is plotted: no sample series sneaks onto the chart.
    const opt = chartOption();
    expect(opt.series ?? []).toEqual([]);
    const alert: HTMLElement = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alert.textContent).toContain('could not be loaded');
    expect(fixture.nativeElement.querySelector('.chart-container').classList).toContain('chart-hidden');
  });

  it('retries from the error state', async () => {
    await create(throwError(() => ({ status: 500 })));
    fixture.detectChanges();
    api.getCompetencyHeatmap.and.returnValue(of(DATA));
    (fixture.nativeElement.querySelector('[role="alert"] button') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(component.status()).toBe('ready');
    expect(chartOption().xAxis[0].data).toEqual(DATA.work_roles);
  });

  it('shows an empty state when there are no assessments', async () => {
    await create(of({ categories: [], work_roles: [], values: [] }));
    fixture.detectChanges();
    expect(component.status()).toBe('empty');
    expect(fixture.nativeElement.textContent).toContain('No competency assessments recorded yet');
  });

  it('disconnects its ResizeObserver and disposes the chart on destroy', async () => {
    const disconnect = spyOn(ResizeObserver.prototype, 'disconnect').and.callThrough();
    await create(of(DATA));
    const chart = (component as any).chartInstance;
    const dispose = spyOn(chart, 'dispose').and.callThrough();
    fixture.destroy();
    expect(disconnect).toHaveBeenCalled();
    expect(dispose).toHaveBeenCalled();
  });

  it('does nothing before the chart is initialised', () => {
    fixture = TestBed.createComponent(CompetencyHeatmapComponent);
    fixture.componentInstance.renderChart();
    expect(api.getCompetencyHeatmap).not.toHaveBeenCalled();
  });
});
