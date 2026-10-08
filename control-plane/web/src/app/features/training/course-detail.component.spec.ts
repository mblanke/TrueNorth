import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';

import { signal } from '@angular/core';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { CourseDetailComponent } from './course-detail.component';

const OUTLINE = {
  id: 'c-1', course_code: 'C101', name: 'C101 — Log triage', description: 'Find the beacon',
  difficulty: 'beginner', duration_hours: 4, is_published: true, institution: '', dp_order: 1,
  term_label: '', provenance: '', status: '', tags: [], modules: [],
};

const MOODLE = { id: 'p1', platform_type: 'moodle', is_active: true, lti_issuer: 'https://moodle.test' };

describe('CourseDetailComponent', () => {
  let fixture: ComponentFixture<CourseDetailComponent>;
  let api: jasmine.SpyObj<ApiService>;
  let submitted: HTMLFormElement[];

  function setup(
    id: string | null = 'c-1',
    opts: { staff?: boolean; platforms?: unknown } = { staff: true, platforms: of([MOODLE]) },
  ): void {
    api = jasmine.createSpyObj<ApiService>('ApiService', ['get', 'post']);
    api.get.and.callFake(((path: string) =>
      path === '/integrations/platforms' ? opts.platforms ?? of([]) : of(OUTLINE)) as any);
    TestBed.configureTestingModule({
      imports: [CourseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: { canReadIntegrations: signal(!!opts.staff) } },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap(id ? { id } : {}) } } },
      ],
    });
    fixture = TestBed.createComponent(CourseDetailComponent);
    fixture.detectChanges();
  }

  function moodleButton(): HTMLButtonElement {
    return [...fixture.nativeElement.querySelectorAll('button')]
      .find((b: HTMLButtonElement) => b.textContent?.includes('Open in Moodle')) as HTMLButtonElement;
  }

  beforeEach(() => {
    submitted = [];
    spyOn(HTMLFormElement.prototype, 'submit').and.callFake(function (this: HTMLFormElement) {
      submitted.push(this);
    });
  });

  it('loads the outline for the id in the URL', () => {
    setup();
    expect(api.get).toHaveBeenCalledWith('/courses/c-1/outline');
    expect(fixture.nativeElement.textContent).toContain('Log triage');
  });

  it('shows Open in Moodle when the tenant has an active Moodle with an LTI issuer', () => {
    setup();
    expect(api.get).toHaveBeenCalledWith('/integrations/platforms');
    expect(moodleButton()).toBeDefined();
  });

  it('hides Open in Moodle when the tenant has no Moodle', () => {
    setup('c-1', { staff: true, platforms: of([{ ...MOODLE, platform_type: 'offsec' }]) });
    expect(moodleButton()).toBeUndefined();
  });

  it('hides Open in Moodle for an inactive or unconfigured Moodle', () => {
    setup('c-1', { staff: true, platforms: of([{ ...MOODLE, is_active: false }, { ...MOODLE, lti_issuer: null }]) });
    expect(moodleButton()).toBeUndefined();
  });

  it('hides Open in Moodle when the platform lookup fails', () => {
    setup('c-1', { staff: true, platforms: throwError(() => ({ status: 500 })) });
    expect(moodleButton()).toBeUndefined();
  });

  it('hides Open in Moodle from a Student, for whom no API says whether a Moodle exists', () => {
    setup('c-1', { staff: false });
    expect(api.get).not.toHaveBeenCalledWith('/integrations/platforms');
    expect(moodleButton()).toBeUndefined();
  });

  it('reports a missing id without calling the API', () => {
    setup(null);
    expect(api.get).not.toHaveBeenCalled();
    expect(fixture.nativeElement.textContent).toContain('No course id');
  });

  it('POSTs the SSO ticket to Moodle in a new tab, never in the URL', () => {
    const tab = { opener: {}, close: jasmine.createSpy('close') } as unknown as Window;
    spyOn(window, 'open').and.returnValue(tab);
    setup();
    api.post.and.returnValue(of({ action: 'http://moodle.test/local/truenorth/sso.php', token: 'tkn' }) as any);

    moodleButton().click();

    expect(api.post).toHaveBeenCalledWith('/integrations/moodle/sso', { course_id: 'c-1' });
    expect(tab.opener).toBeNull();
    expect(submitted.length).toBe(1);
    const form = submitted[0];
    expect(form.method.toLowerCase()).toBe('post');
    expect(form.action).toBe('http://moodle.test/local/truenorth/sso.php');
    expect(form.target).toBe('tn-moodle');
    expect((form.querySelector('input[name="token"]') as HTMLInputElement).value).toBe('tkn');
    expect(form.action).not.toContain('tkn');
  });

  it('closes the tab and shows the reason when Moodle cannot be opened', () => {
    const tab = { opener: null, close: jasmine.createSpy('close') } as unknown as Window;
    spyOn(window, 'open').and.returnValue(tab);
    setup();
    api.post.and.returnValue(throwError(() => ({ error: { detail: 'You are not enrolled in this course' } })));

    moodleButton().click();
    fixture.detectChanges();

    expect(tab.close).toHaveBeenCalled();
    expect(submitted.length).toBe(0);
    expect(fixture.nativeElement.querySelector('.moodle-err').textContent).toContain('not enrolled');
    expect(moodleButton().disabled).toBeFalse();
  });

  it('falls back to the same tab when pop-ups are blocked', () => {
    spyOn(window, 'open').and.returnValue(null);
    setup();
    api.post.and.returnValue(of({ action: 'http://moodle.test/local/truenorth/sso.php', token: 't' }) as any);
    moodleButton().click();
    expect(submitted[0].target).toBe('_self');
  });
});
