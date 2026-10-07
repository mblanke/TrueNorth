import { Component, Directive, Input, signal } from '@angular/core';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { NgxEchartsDirective } from 'ngx-echarts';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { ThemeService } from '@core/services/theme.service';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';
import { MyProgressComponent } from './my-progress.component';

@Component({ selector: 'tn-lottie', template: '' })
class LottieStubComponent {
  @Input() name = '';
  @Input() size = 0;
}

/** Stands in for ngx-echarts so no chart engine loads; the options are still bound and inspectable. */
// eslint-disable-next-line @angular-eslint/directive-selector -- must match the third-party [echarts] attribute it replaces
@Directive({ selector: '[echarts]' })
class EchartsStubDirective {
  @Input() options: unknown;
}

describe('MyProgressComponent', () => {
  let fixture: ComponentFixture<MyProgressComponent>;
  let component: MyProgressComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  const DAY = 24 * 60 * 60 * 1000;
  const TRANSCRIPT = {
    total_hours: 42,
    entries: [
      { source: 'truenorth', activity_type: 'exercise', title: 'Ransomware IR', score: 88, grade: null, completed_at: '2026-09-10T00:00:00Z', competencies_earned: [] },
      { source: 'moodle', activity_type: 'course', title: 'Intro to SIEM', score: null, grade: 'A', completed_at: null, competencies_earned: [] },
    ],
    certifications: [
      { cert_name: 'Sec+', issuer: 'CompTIA', credential_id: 'c-1', issued_at: '2024-01-01', expires_at: new Date(Date.now() - DAY).toISOString(), status: 'active' },
      { cert_name: 'GCIH', issuer: 'GIAC', credential_id: 'c-2', issued_at: '2025-01-01', expires_at: new Date(Date.now() + 10 * DAY).toISOString(), status: 'active' },
      { cert_name: 'CISSP', issuer: 'ISC2', credential_id: 'c-3', issued_at: '2025-06-01', expires_at: null, status: 'active' },
    ],
  };
  const PROGRESS = {
    avg_score: 76.6, total_exercises: 5,
    strongest_areas: ['Detection'], weakest_areas: ['Forensics'],
    competency_trend: [{ category: 'Detection', avg_delta: 4, count: 3 }, { category: 'Forensics', avg_delta: -2, count: 1 }],
  };
  const RECS = [{
    id: 'r1', summary: 'Focus on forensics', strengths: [], gaps: ['Memory analysis'],
    recommendations: [{ area: 'Forensics', action: 'Take DFIR 201' }], next_milestone: 'Pass DFIR quiz',
    generated_at: '2026-10-01T00:00:00Z',
  }];
  const ASSESSMENTS = [{
    id: 'a1', exercise_id: 'e1', raw_score: 8, max_score: 10,
    competency_mappings: [{ category: 'Detection', delta: 5 }], assessed_at: '2026-10-02T00:00:00Z',
  }];
  const PROFILE = {
    assertions: [
      { proficiency: 'advanced', competency: { category: 'Analyze' } },
      { proficiency: 'novice', competency: { category: 'Investigate' } },
      { proficiency: 'expert', competency: { category: 'Protect' } },
    ],
  };

  let responses: Record<string, unknown>;

  beforeEach(async () => {
    responses = {
      '/users/u-7/transcript': of(TRANSCRIPT),
      '/adaptive/users/u-7/progress': of(PROGRESS),
      '/adaptive/users/u-7/recommendations': of(RECS),
      '/adaptive/users/u-7/auto-assessments': of(ASSESSMENTS),
    };
    api = jasmine.createSpyObj('ApiService', ['get', 'post', 'getCompetencyProfile']);
    api.get.and.callFake(((path: string) => (responses[path] ?? of(null))) as any);
    api.post.and.returnValue(of({}));
    api.getCompetencyProfile.and.returnValue(of(PROFILE));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);

    await TestBed.configureTestingModule({
      imports: [MyProgressComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { userId: signal('u-7') } },
        { provide: ThemeService, useValue: { activeTheme: signal('dark') } },
      ],
    })
      .overrideComponent(MyProgressComponent, {
        remove: { imports: [LottieIconComponent, NgxEchartsDirective] },
        add: { imports: [LottieStubComponent, EchartsStubDirective] },
      })
      .compileComponents();
  });

  function create(): void {
    fixture = TestBed.createComponent(MyProgressComponent);
    component = fixture.componentInstance;
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

  const stats = () => Array.from(el.querySelectorAll('.stat-value')).map(v => v.textContent?.trim());

  it('creates and loads every section from the signed-in user’s endpoints', () => {
    create();
    expect(component).toBeTruthy();
    const paths = api.get.calls.allArgs().map(a => a[0]);
    expect(paths).toEqual(jasmine.arrayWithExactContents([
      '/users/u-7/transcript',
      '/adaptive/users/u-7/progress',
      '/adaptive/users/u-7/recommendations',
      '/adaptive/users/u-7/auto-assessments',
    ]));
    expect(api.getCompetencyProfile).toHaveBeenCalledWith('u-7');
  });

  it('renders the headline stats from the transcript and progress summary', () => {
    create();
    expect(stats()).toEqual(['42', '2', '3', '77%']);
  });

  it('renders transcript rows with grade, score or a dash', () => {
    create();
    const rows = Array.from(el.querySelectorAll('tr.mat-mdc-row'));
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('Ransomware IR');
    expect(rows[0].textContent).toContain('88%');
    expect(rows[1].textContent).toContain('A');
    expect(rows[1].querySelector('.source-badge')?.getAttribute('data-source')).toBe('moodle');
  });

  it('builds the capability radar from three or more categories', () => {
    create();
    expect(el.querySelector('.radar-card')).not.toBeNull();
    const radar: any = component.radarOption();
    expect(radar.radar.indicator.map((i: any) => i.name)).toEqual(['Analyze', 'Investigate', 'Protect']);
    expect(radar.series[0].data[0].value).toEqual([4, 1, 5]);
  });

  it('hides the radar when fewer than three categories are assessed', () => {
    api.getCompetencyProfile.and.returnValue(of({ assertions: PROFILE.assertions.slice(0, 2) }));
    create();
    expect(component.radarOption()).toBeNull();
    expect(el.querySelector('.radar-card')).toBeNull();
  });

  it('flags expired and soon-expiring certifications', async () => {
    create();
    await openTab('Certifications');
    const cards = Array.from(el.querySelectorAll('.card-grid mat-card'));
    expect(cards.length).toBe(3);
    expect(cards[0].querySelector('.status-chip')?.textContent).toContain('expired');
    expect(cards[1].querySelector('.status-chip')?.textContent).toContain('expires soon');
    expect(cards[2].querySelector('.status-chip')).toBeNull();
  });

  it('shows strengths, gaps, trend and recommendations on AI Insights', async () => {
    create();
    await openTab('AI Insights');
    expect(el.textContent).toContain('Detection');
    expect(el.textContent).toContain('Forensics');
    expect(el.querySelector('.trend-card')).not.toBeNull();
    const trend: any = component.trendOption();
    expect(trend.yAxis.data).toEqual(['Forensics', 'Detection']);
    const rec = el.querySelector('.rec-card')!;
    expect(rec.textContent).toContain('Focus on forensics');
    expect(rec.textContent).toContain('Take DFIR 201');
    expect(rec.textContent).toContain('Pass DFIR quiz');
  });

  it('lists auto-assessments with their competency deltas', async () => {
    create();
    await openTab('Assessments');
    const card = el.querySelector('.assessment-card')!;
    expect(card.textContent).toContain('Score: 8/10');
    expect(card.querySelector('.competency-score')?.textContent?.trim()).toBe('+5');
  });

  it('degrades to empty states without crashing when every request fails', async () => {
    for (const k of Object.keys(responses)) responses[k] = throwError(() => ({ status: 500 }));
    api.getCompetencyProfile.and.returnValue(throwError(() => ({ status: 500 })));
    create();
    expect(stats()).toEqual(['0', '0', '0', '-']);
    expect(el.textContent).toContain('No transcript entries yet');
    expect(el.querySelector('.radar-card')).toBeNull();

    await openTab('AI Insights');
    expect(el.textContent).toContain('No recommendations yet');
    await openTab('Assessments');
    expect(el.textContent).toContain('No auto-assessments yet');
  });

  it('starts recommendation generation and reloads recommendations later', fakeAsync(() => {
    create();
    const before = api.get.calls.allArgs().filter(a => a[0] === '/adaptive/users/u-7/recommendations').length;
    component.generateRecommendation();
    expect(api.post).toHaveBeenCalledWith('/adaptive/users/u-7/recommendations', {});
    expect(notify.success).toHaveBeenCalledWith('Recommendation generation started');
    expect(component.generatingRec()).toBeFalse();
    tick(3000);
    const after = api.get.calls.allArgs().filter(a => a[0] === '/adaptive/users/u-7/recommendations').length;
    expect(after).toBe(before + 1);
  }));

  it('reports a failed recommendation request and re-enables the button', () => {
    create();
    api.post.and.returnValue(throwError(() => ({ status: 500 })));
    component.generateRecommendation();
    expect(notify.error).toHaveBeenCalledWith('Failed to generate recommendation');
    expect(component.generatingRec()).toBeFalse();
  });
});
