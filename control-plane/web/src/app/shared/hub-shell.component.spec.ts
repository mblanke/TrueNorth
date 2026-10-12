import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { environment } from '@env/environment';
import { AuthService, UserRole } from '@core/services/auth.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { HubShellComponent } from './hub-shell.component';

const STATUS = `${environment.apiUrl}/arc2/status`;
const AUTHORING = {
  title: 'Authoring Studio',
  tabs: [{ label: 'Course Studio', path: 'studio', requires: 'arc2' }, { label: 'Ranges', path: 'ranges' }],
};

describe('HubShellComponent', () => {
  let http: HttpTestingController;

  function render(data: object, role: UserRole = 'instructor'): HTMLElement {
    TestBed.configureTestingModule({
      imports: [HubShellComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { data } } },
        { provide: AuthService, useValue: fakeAuth(role) }],
    });
    http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(HubShellComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const labels = (el: HTMLElement) => Array.from(el.querySelectorAll('.hub-tabs a')).map(a => a.textContent!.trim());

  it('hides the ARC² Course Studio tab when the server has it off', () => {
    const el = render(AUTHORING);
    expect(labels(el)).toEqual(['Ranges']);  // not shown while unknown either
    http.expectOne(STATUS).flush({ enabled: false, reason: 'ARC² Course Studio is not enabled on this server.' });
    TestBed.tick();
    expect(labels(el)).toEqual(['Ranges']);
    http.verify();
  });

  it('shows it when the server has it on', () => {
    const el = render(AUTHORING);
    http.expectOne(STATUS).flush({ enabled: true, reason: null });
    TestBed.tick();
    expect(labels(el)).toEqual(['Course Studio', 'Ranges']);
  });

  it('hides it if the check fails', () => {
    const el = render(AUTHORING);
    http.expectOne(STATUS).flush({ detail: 'Bad gateway' }, { status: 502, statusText: 'Bad Gateway' });
    TestBed.tick();
    expect(labels(el)).toEqual(['Ranges']);
  });

  // R0-B: tabs follow the same PAGE_ACCESS table as the child routes' pageGuard.
  const AUTHORING_TABS = {
    title: 'Authoring Studio',
    tabs: [
      { label: 'Ranges', path: 'ranges', access: 'authoringRanges' },
      { label: 'Scenarios', path: 'scenarios', access: 'authoringScenarios' },
      { label: 'Content', path: 'content', access: 'authoringContent' },
      { label: 'Courses', path: 'courses', access: 'authoringCourses' },
    ],
  };

  it('gives range ops Ranges and Content (templates) only', () => {
    expect(labels(render(AUTHORING_TABS, 'range_ops'))).toEqual(['Ranges', 'Content']);
  });

  it('gives an instructor every authoring tab', () => {
    expect(labels(render(AUTHORING_TABS, 'instructor'))).toEqual(['Ranges', 'Scenarios', 'Content', 'Courses']);
  });

  describe('fictional previews tab', () => {
    const LEARNING = { title: 'Learning', tabs: [{ label: 'Courses', path: 'courses' },
      { label: 'Demo previews', path: 'previews', requires: 'demo' }] };
    const configured = environment.demoPreviews;
    afterEach(() => (environment.demoPreviews = configured));

    it('is hidden unless the deployment turns the demo on', () => {
      environment.demoPreviews = undefined;
      expect(labels(render(LEARNING, 'student'))).toEqual(['Courses']);
    });

    it('is shown when TN_DEMO_PREVIEWS turned it on', () => {
      environment.demoPreviews = true;
      expect(labels(render(LEARNING, 'student'))).toEqual(['Courses', 'Demo previews']);
    });
  });

  it('does not ask about ARC² in a hub without that tab', () => {
    const el = render({ title: 'Learning', tabs: [{ label: 'Courses', path: 'courses' }] });
    expect(labels(el)).toEqual(['Courses']);
    http.expectNone(STATUS);
    http.verify();
  });
});
