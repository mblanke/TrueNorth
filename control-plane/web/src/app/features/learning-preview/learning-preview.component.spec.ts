import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Observable, of, throwError } from 'rxjs';

import { AuthService } from '@core/services/auth.service';
import { CourseStudioApiService, LearningPlatform } from '@core/services/course-studio-api.service';
import { LearningPreviewComponent, registeredMoodleUrl } from './learning-preview.component';

const platform = (over: Partial<LearningPlatform>): LearningPlatform =>
  ({
    id: 'p1', name: 'Unit Moodle', slug: 'moodle', platform_type: 'moodle',
    base_url: 'https://moodle.unit.example/', auth_type: 'lti13', is_active: true, tenant_id: 't1',
    ...over,
  }) as LearningPlatform;

describe('LearningPreviewComponent', () => {
  let harness: RouterTestingHarness;
  let instructor: ReturnType<typeof signal<boolean>>;
  let platforms: jasmine.Spy<() => Observable<LearningPlatform[]>>;

  async function setUp(routeData: Record<string, string> = {}): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule],
      providers: [
        provideRouter(
          [{ path: 'preview', component: LearningPreviewComponent, data: routeData }],
          withComponentInputBinding(),
        ),
        { provide: AuthService, useValue: { isInstructor: instructor } },
        { provide: CourseStudioApiService, useValue: { platforms } },
      ],
    }).compileComponents();
    harness = await RouterTestingHarness.create();
  }

  beforeEach(async () => {
    instructor = signal(false);
    platforms = jasmine.createSpy('platforms').and.returnValue(of([]));
    await setUp();
  });

  async function open(url: string): Promise<{ cmp: LearningPreviewComponent; el: HTMLElement }> {
    const cmp = await harness.navigateByUrl(url, LearningPreviewComponent);
    harness.detectChanges();
    return { cmp, el: harness.routeNativeElement as HTMLElement };
  }

  const current = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('nav a[aria-current="page"]')).map(a => a.textContent?.trim());

  it('creates and defaults to the LMS development preview', async () => {
    const { cmp, el } = await open('/preview');
    expect(cmp).toBeTruthy();
    expect(cmp.view()).toBe('development');
    const frame = el.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('assets/previews/development.html');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(current(el)).toEqual(['LMS development']);
    expect(el.querySelector('.notice')).not.toBeNull();
  });

  it('shows the commander readiness preview for ?view=readiness', async () => {
    const { cmp, el } = await open('/preview?view=readiness');
    expect(cmp.view()).toBe('readiness');
    expect(el.querySelector('iframe')!.getAttribute('src')).toBe('assets/previews/readiness.html');
    expect(current(el)).toEqual(['Commander readiness']);
  });

  it('shows the Moodle panel with no iframe for ?view=moodle', async () => {
    const { cmp, el } = await open('/preview?view=moodle');
    expect(cmp.view()).toBe('moodle');
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('article h2')?.textContent).toContain('Moodle test LMS');
    expect(current(el)).toEqual(['Moodle']);
  });

  it('never links to a hard-coded localhost Moodle; with nothing configured it says so', async () => {
    const { cmp, el } = await open('/preview?view=moodle');
    expect(cmp.moodleLink()).toBeUndefined();
    expect(el.querySelector('a[target="_blank"]')).toBeNull();
    expect(el.querySelector('.unconfigured')?.textContent).toContain('No Moodle site is registered');
    expect(el.innerHTML).not.toContain('localhost:8083');
    // A Student cannot read integrations, so the page does not ask.
    expect(platforms).not.toHaveBeenCalled();
  });

  it('links to the registered Moodle platform for instructors', async () => {
    TestBed.resetTestingModule();
    instructor.set(true);
    platforms.and.returnValue(of([platform({ platform_type: 'offsec', base_url: 'https://offsec.example/' }), platform({})]));
    await setUp();
    const { cmp, el } = await open('/preview?view=moodle');
    expect(platforms).toHaveBeenCalled();
    expect(cmp.moodleLink()).toBe('https://moodle.unit.example/');
    const external = el.querySelector<HTMLAnchorElement>('a[target="_blank"]')!;
    expect(external.getAttribute('href')).toBe('https://moodle.unit.example/');
    expect(external.getAttribute('rel')).toContain('noopener');
  });

  it('falls back to the moodleUrl input when no platform is registered or the lookup fails', async () => {
    TestBed.resetTestingModule();
    instructor.set(true);
    platforms.and.returnValue(throwError(() => new Error('403')));
    await setUp({ moodleUrl: 'https://lms.range.example/' });
    const { cmp, el } = await open('/preview?view=moodle');
    expect(cmp.moodleLink()).toBe('https://lms.range.example/');
    expect(el.querySelector<HTMLAnchorElement>('a[target="_blank"]')!.getAttribute('href'))
      .toBe('https://lms.range.example/');
  });

  it('only accepts active Moodle platforms with an http(s) base URL', () => {
    expect(registeredMoodleUrl([])).toBeUndefined();
    expect(registeredMoodleUrl([platform({ is_active: false })])).toBeUndefined();
    expect(registeredMoodleUrl([platform({ base_url: 'javascript:alert(1)' })])).toBeUndefined();
    expect(registeredMoodleUrl([platform({ base_url: ' http://moodle:8080 ' })])).toBe('http://moodle:8080');
  });

  it('falls back to development for an unknown view', async () => {
    const { cmp, el } = await open('/preview?view=bogus');
    expect(cmp.view()).toBe('development');
    expect(el.querySelector('iframe')!.getAttribute('src')).toBe('assets/previews/development.html');
  });

  it('switches view when the query param changes on the same route', async () => {
    const { cmp } = await open('/preview?view=moodle');
    expect(cmp.view()).toBe('moodle');
    const again = await harness.navigateByUrl('/preview?view=readiness', LearningPreviewComponent);
    expect(again).toBe(cmp);
    expect(cmp.view()).toBe('readiness');
  });

  it('navigates via the workspace links', async () => {
    const { cmp, el } = await open('/preview');
    const moodleLink = Array.from(el.querySelectorAll<HTMLAnchorElement>('nav a'))
      .find(a => a.textContent?.includes('Moodle'))!;
    expect(moodleLink.getAttribute('href')).toBe('/preview?view=moodle');
    moodleLink.click();
    await harness.fixture.whenStable();
    harness.detectChanges();
    expect(cmp.view()).toBe('moodle');
  });
});
