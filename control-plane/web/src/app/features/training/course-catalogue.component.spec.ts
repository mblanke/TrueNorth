import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';
import { ApiService, Enrollment } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { fakeAuth } from '@core/auth/fake-auth';

import { CourseCatalogueComponent, catalogueStages } from './course-catalogue.component';
import { CurriculumMap, CurriculumMapService, NodeCourse, QualNode } from '@core/services/curriculum-map.service';

function course(code: string, term: string, published = true): NodeCourse {
  return {
    course_id: `id-${code}`, course_code: code, name: `${code} — Course ${code}`, institution: 'Algonquin',
    term_code: term, term_label: `Term ${term}`, term_start: '', duration_hours: 40, difficulty: 'beginner',
    is_published: published, delivers: [], delivers_cross_dp: false,
  };
}

function node(code: string, dp: number, courses: NodeCourse[]): QualNode {
  return {
    qsp_code: code, nqual: code, title: code, dp_order: dp, track: 't', track_key: 't', rank_level: '',
    po_count: 0, gate_count: 0, total_minutes: 0, timed_po_count: 0, course_hours: 0, state: 'available',
    progress: { completed: 0, in_progress: 0, total: 0, pct: 0 }, objectives: [], courses, course_count: courses.length,
  };
}

const SHARED = course('CST300', 'Y3');
const MAP: CurriculumMap = {
  stages: [
    { dp_order: 2, rank_level: 'Cpl', label: 'Operator', planned: false },
    { dp_order: 1, rank_level: 'Pte', label: 'Foundations', planned: false },
    { dp_order: 3, rank_level: 'Sgt', label: 'Team lead', planned: true },
  ],
  tracks: [],
  nodes: [
    // The ladder and a specialty stream in the same period both carry CST300.
    node('LADDER1', 1, [course('CST101', 'Y1'), course('CST102', 'Y1', false), SHARED]),
    node('SPEC1', 1, [SHARED]),
    node('LADDER2', 2, [course('CYB201', 'F')]),
  ],
  edges: [],
  course_edges: [],
  learner: { current_qsp_code: 'LADDER2', current_po_code: null },
};

describe('catalogueStages', () => {
  it('groups by period in order, then by term, and skips empty periods', () => {
    const stages = catalogueStages(MAP);
    expect(stages.map(s => s.stage.dp_order)).toEqual([1, 2]);
    expect(stages[0].terms.map(t => t.code)).toEqual(['Y1', 'Y3']);
  });

  it('lists a course once per period even when two qualifications carry it', () => {
    const [dp1] = catalogueStages(MAP);
    expect(dp1.count).toBe(3);
  });

  it('filters by code, name or term', () => {
    expect(catalogueStages(MAP, 'cyb2').map(s => s.count)).toEqual([1]);
    expect(catalogueStages(MAP, 'term y3')[0].count).toBe(1);
    expect(catalogueStages(MAP, 'nothing like this')).toEqual([]);
  });
});

describe('CourseCatalogueComponent', () => {
  let fixture: ComponentFixture<CourseCatalogueComponent>;
  let el: HTMLElement;
  let navigate: jasmine.Spy;
  let api: jasmine.SpyObj<ApiService>;

  async function render(map: CurriculumMap, query: Record<string, string> = {}, mine: Enrollment[] = []): Promise<void> {
    api = jasmine.createSpyObj<ApiService>('ApiService', ['myEnrollments', 'enrollInCourse']);
    api.myEnrollments.and.returnValue(of(mine));
    api.enrollInCourse.and.callFake((courseId: string) => of(enrolment(courseId)));
    await TestBed.configureTestingModule({
      imports: [CourseCatalogueComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: CurriculumMapService, useValue: { map: () => of(map), refresh: () => of(map) } },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(query) } } },
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: fakeAuth('student') },
      ],
    }).compileComponents();
    navigate = spyOn(TestBed.inject(Router), 'navigate').and.resolveTo(true);
    fixture = TestBed.createComponent(CourseCatalogueComponent);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const expanded = () =>
    Array.from(el.querySelectorAll<HTMLElement>('mat-expansion-panel.stage.mat-expanded')).map(p => p.dataset['stage']);

  it('opens on the learner’s own period and leaves the rest closed', async () => {
    await render(MAP);
    expect(expanded()).toEqual(['2']);
  });

  it('opens every period with a match while searching', async () => {
    await render(MAP);
    const input = el.querySelector<HTMLInputElement>('input[type="search"]')!;
    input.value = 'CST';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(expanded()).toEqual(['1']);
    expect(el.querySelectorAll('[data-course]').length).toBe(3);
  });

  it('marks an unpublished course as a draft', async () => {
    await render(MAP);
    const input = el.querySelector<HTMLInputElement>('input[type="search"]')!;
    input.value = 'CST102';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.querySelector('[data-course="CST102"] .draft')?.textContent).toContain('Draft');
    expect(el.querySelector('[data-course="CST101"]')).toBeNull();
  });

  it('shows an empty state when there is nothing to list', async () => {
    await render({ ...MAP, nodes: [] });
    expect(el.querySelector('tn-empty-state')?.textContent).toContain('No courses in the catalogue yet');
  });

  it('sends ?course=<id> (an LTI course launch) straight to that course', async () => {
    await render(MAP, { course: 'abc-123' });
    expect(navigate).toHaveBeenCalledWith(['/learning/courses', 'abc-123'], { replaceUrl: true });
  });

  // ── Self-enrolment (R0-B) ────────────────────────────────────────
  describe('Enrol', () => {
    async function search(q: string, mine: Enrollment[] = []): Promise<void> {
      await render(MAP, {}, mine);
      const input = el.querySelector<HTMLInputElement>('input[type="search"]')!;
      input.value = q;
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    }
    const rowOf = (code: string) => el.querySelector(`[data-course="${code}"]`)!.closest('li')!;
    const enrolButton = (code: string) => rowOf(code).querySelector<HTMLButtonElement>('[data-testid="enrol"]');

    it('offers Enrol on a published course, outside the course link, and not on a draft', async () => {
      await search('CST10');
      expect(enrolButton('CST101')).not.toBeNull();
      expect(enrolButton('CST101')!.closest('a')).toBeNull();
      expect(enrolButton('CST101')!.getAttribute('aria-label')).toBe('Enrol on Course CST101');
      expect(enrolButton('CST102')).toBeNull();
    });

    it('shows the courses the server says I am enrolled on', async () => {
      await search('CST101', [enrolment('id-CST101')]);
      expect(api.myEnrollments).toHaveBeenCalled();
      expect(rowOf('CST101').querySelector('[data-testid="enrolled"]')).not.toBeNull();
      expect(enrolButton('CST101')).toBeNull();
    });

    it('POSTs /courses/{id}/enroll for myself and shows Enrolled once the server confirms', async () => {
      await search('CST101');
      enrolButton('CST101')!.click();
      fixture.detectChanges();
      expect(api.enrollInCourse).toHaveBeenCalledOnceWith('id-CST101', 'u-self');
      expect(rowOf('CST101').querySelector('[data-testid="enrolled"]')).not.toBeNull();
    });

    it('sends one request for a double click', async () => {
      const pending = new Subject<Enrollment>();
      await search('CST101');
      api.enrollInCourse.and.returnValue(pending);
      const button = enrolButton('CST101')!;
      button.click();
      fixture.detectChanges();
      button.click();
      expect(api.enrollInCourse).toHaveBeenCalledTimes(1);
      expect(button.disabled).toBeTrue();
      pending.next(enrolment('id-CST101'));
      pending.complete();
      fixture.detectChanges();
      expect(rowOf('CST101').querySelector('[data-testid="enrolled"]')).not.toBeNull();
    });

    it('shows the server\'s refusal and stays un-enrolled', async () => {
      await search('CST101');
      api.enrollInCourse.and.returnValue(throwError(() =>
        ({ status: 409, error: { detail: 'This course is retired and no longer takes enrolments' } })));
      enrolButton('CST101')!.click();
      fixture.detectChanges();
      expect(rowOf('CST101').querySelector('[role="alert"]')?.textContent).toContain('retired');
      expect(rowOf('CST101').querySelector('[data-testid="enrolled"]')).toBeNull();
      expect(enrolButton('CST101')!.disabled).toBeFalse();
    });
  });
});

function enrolment(courseId: string): Enrollment {
  return {
    id: `e-${courseId}`, user_id: 'u-self', course_id: courseId, tenant_id: 't', status: 'enrolled',
    enrolled_at: '2026-10-12T00:00:00Z', final_score: 0, max_score: 0, created_at: '2026-10-12T00:00:00Z',
  } as Enrollment;
}
