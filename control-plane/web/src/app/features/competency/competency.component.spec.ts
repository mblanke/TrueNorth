import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { ThemeService } from '@core/services/theme.service';
import { CompetencyComponent } from './competency.component';
import { CompetencyHeatmapComponent } from './competency-heatmap.component';

/** The heatmap has its own spec; here it is replaced so no ECharts instance is created. */
@Component({ selector: 'tn-competency-heatmap', template: '' })
class HeatmapStubComponent {}

describe('CompetencyComponent', () => {
  let fixture: ComponentFixture<CompetencyComponent>;
  let component: CompetencyComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ApiService>;
  let renderCharts: jasmine.Spy;
  let alertSpy: jasmine.Spy;

  const FRAMEWORK = [
    { id: 'c1', code: 'K0001', name: 'Networking concepts', framework: 'nice', category: 'Knowledge', level: 1 },
    { id: 'c2', code: 'S0002', name: 'Log analysis', framework: 'nice', category: 'Skill', level: 2 },
  ];
  const PROFILE = {
    assertions: [{ competency_id: 'c1', proficiency: 'intermediate', assessed_at: '2026-09-01', source: 'quiz' }],
    by_framework: { nice: 4, dcwf: 2 },
    by_proficiency: { novice: 1, intermediate: 5 },
  };
  const GAPS = [
    {
      competency: FRAMEWORK[1], current_proficiency: null, required_proficiency: 'advanced',
      recommended_courses: ['SOC 101', 'Log Hunting'],
    },
  ];

  let responses: Record<string, unknown>;

  beforeEach(async () => {
    responses = {
      '/competency/frameworks': of(FRAMEWORK),
      '/competency/users/u-1/profile': of(PROFILE),
    };
    api = jasmine.createSpyObj('ApiService', ['get', 'post']);
    api.get.and.callFake(((path: string) => {
      const key = Object.keys(responses).find(k => path.startsWith(k));
      return (key ? responses[key] : of(null)) as any;
    }) as any);
    api.post.and.returnValue(of({}));
    alertSpy = spyOn(window, 'alert');

    await TestBed.configureTestingModule({
      imports: [CompetencyComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: { userId: signal('u-1') } },
        { provide: ThemeService, useValue: { activeTheme: signal('dark') } },
      ],
    })
      .overrideComponent(CompetencyComponent, {
        remove: { imports: [CompetencyHeatmapComponent] },
        add: { imports: [HeatmapStubComponent] },
      })
      .compileComponents();
  });

  async function create(): Promise<void> {
    fixture = TestBed.createComponent(CompetencyComponent);
    component = fixture.componentInstance;
    renderCharts = spyOn(component as any, 'renderCharts').and.resolveTo();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  async function openTab(label: string): Promise<void> {
    const tab = Array.from(el.querySelectorAll<HTMLElement>('[role="tab"]'))
      .find(t => t.textContent?.includes(label))!;
    tab.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('creates and loads the framework and the signed-in user’s profile', async () => {
    await create();
    expect(component).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith('/competency/frameworks');
    expect(api.get).toHaveBeenCalledWith('/competency/users/u-1/profile');
    expect(component.competencies().length).toBe(2);
  });

  it('renders framework and proficiency counts and draws the charts', async () => {
    await create();
    const values = Array.from(el.querySelectorAll('.stat-value')).map(v => v.textContent?.trim());
    const labels = Array.from(el.querySelectorAll('.stat-label')).map(v => v.textContent?.trim());
    expect(values).toEqual(['4', '2', '1', '5']);
    expect(labels).toEqual(['NICE', 'DCWF', 'novice', 'intermediate']);
    expect(renderCharts).toHaveBeenCalledWith(PROFILE);
  });

  it('shows the no-data message when the profile request fails', async () => {
    responses['/competency/users/u-1/profile'] = throwError(() => ({ status: 500 }));
    await create();
    expect(component.profile()).toBeNull();
    expect(el.textContent).toContain('No competency data yet');
    expect(renderCharts).not.toHaveBeenCalled();
  });

  it('lists competencies in the framework browser', async () => {
    await create();
    await openTab('Framework Browser');
    const rows = Array.from(el.querySelectorAll('tr.mat-mdc-row')).map(r => r.textContent);
    expect(rows.length).toBe(2);
    expect(rows[1]).toContain('Log analysis');
  });

  it('prompts to import when the framework request fails', async () => {
    responses['/competency/frameworks'] = throwError(() => ({ status: 503 }));
    await create();
    await openTab('Framework Browser');
    expect(component.competencies()).toEqual([]);
    expect(el.textContent).toContain('No competencies loaded');
  });

  it('imports the NICE framework and reloads the list', async () => {
    await create();
    component.importNice();
    expect(api.post).toHaveBeenCalledWith('/competency/frameworks/import-nice', {});
    expect(alertSpy).toHaveBeenCalledWith('NICE framework imported!');
    expect(api.get.calls.allArgs().filter(a => a[0] === '/competency/frameworks').length).toBe(2);
  });

  it('alerts the server detail when the import fails', async () => {
    await create();
    api.post.and.returnValue(throwError(() => ({ error: { detail: 'Already imported' } })));
    component.importNice();
    expect(alertSpy).toHaveBeenCalledWith('Already imported');
  });

  it('does not request skill gaps until a role is chosen', async () => {
    await create();
    component.loadSkillGaps();
    expect(api.get.calls.allArgs().some(a => String(a[0]).includes('skill-gaps'))).toBeFalse();
  });

  it('loads skill gaps for the chosen work role and renders them', async () => {
    responses['/competency/users/u-1/skill-gaps'] = of(GAPS);
    await create();
    await openTab('Skill Gaps');
    expect(el.querySelector('tn-empty-state')).not.toBeNull();

    component.selectedRole = 'SP-DEV-001';
    component.loadSkillGaps();
    fixture.detectChanges();
    expect(api.get).toHaveBeenCalledWith('/competency/users/u-1/skill-gaps?target_role=SP-DEV-001');
    const card = el.querySelector('.gap-card')!;
    expect(card.textContent).toContain('Log analysis');
    expect(card.textContent).toContain('None');
    expect(card.textContent).toContain('advanced');
    expect(card.textContent).toContain('SOC 101, Log Hunting');
  });

  it('clears skill gaps to the empty state when the request fails', async () => {
    responses['/competency/users/u-1/skill-gaps'] = of(GAPS);
    await create();
    await openTab('Skill Gaps');
    component.selectedRole = 'SP-DEV-001';
    component.loadSkillGaps();
    fixture.detectChanges();
    expect(el.querySelector('.gap-card')).not.toBeNull();

    responses['/competency/users/u-1/skill-gaps'] = throwError(() => ({ status: 500 }));
    component.selectedRole = 'OM-NET-001';
    component.loadSkillGaps();
    fixture.detectChanges();
    expect(component.skillGaps()).toEqual([]);
    expect(el.querySelector('.gap-card')).toBeNull();
    expect(el.querySelector('tn-empty-state')).not.toBeNull();
  });
});
