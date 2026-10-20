import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';

import { CareerMapComponent } from './career-map.component';
import { CourseEdge, CurriculumMap, NodeCourse, QualNode } from '@core/services/curriculum-map.service';

function course(code: string, term: string, start: string, prerequisites: string[] = []): NodeCourse {
  return {
    course_id: `id-${code}`, course_code: code, name: `${code} — Course ${code}`,
    institution: 'Algonquin College', term_code: term, term_label: term, term_start: start,
    duration_hours: 45, difficulty: '', is_published: false, delivers: [],
    delivers_cross_dp: false, prerequisites,
  };
}

const COURSES = [
  course('C101', 'DP1-Y1-F', '2026-09-01'),
  course('C201', 'DP1-Y2-F', '2027-09-01', ['C101']),
  course('C202', 'DP1-Y2-F', '2027-09-01'),
  course('C301', 'DP1-Y3-F', '2028-09-01', ['C201']),
];

function node(courses: NodeCourse[]): QualNode {
  return {
    qsp_code: 'ALJQ', nqual: 'ALJQ', title: 'Cyber Operator', dp_order: 1, track: 'progression',
    track_key: 'progression', rank_level: 'Pte', po_count: 0, gate_count: 0, total_minutes: 0,
    timed_po_count: 0, course_hours: 180, state: 'available',
    progress: { completed: 0, in_progress: 0, total: 0, pct: 0 },
    objectives: [], courses, course_count: courses.length,
  };
}

function mapWith(edges: CourseEdge[]): CurriculumMap {
  return {
    stages: [{ dp_order: 1, rank_level: 'Pte', label: 'Basic occupation', planned: false }],
    tracks: [{ key: 'progression', label: 'Core progression', kind: 'progression' }],
    nodes: [node(COURSES)],
    edges: [],
    course_edges: edges,
    learner: { current_qsp_code: null, current_po_code: null },
  };
}

const DECLARED: CourseEdge[] = [
  { from: 'id-C101', to: 'id-C201' },
  { from: 'id-C201', to: 'id-C301' },
];

const frame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));

describe('CareerMapComponent course prerequisites', () => {
  let fixture: ComponentFixture<CareerMapComponent>;
  let el: HTMLElement;

  async function render(edges: CourseEdge[]): Promise<void> {
    fixture = TestBed.createComponent(CareerMapComponent);
    fixture.componentInstance.map = mapWith(edges);
    fixture.detectChanges();
    el = fixture.nativeElement;
    // The first measure (rAF) and the ResizeObserver's first callback both clear any
    // hover state, so let them run before hovering.
    await frame();
    await frame();
    fixture.detectChanges();
  }

  const chip = (code: string) => el.querySelector<HTMLElement>(`.map .c-chip[data-course="id-${code}"]`)!;

  function hover(code: string): void {
    chip(code).dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    fixture.detectChanges();
  }

  /** [start, end] points of an arrow path: "M x y C ..., ..., x y". */
  function ends(d: string): [number[], number[]] {
    const nums = d.match(/-?\d+(\.\d+)?/g)!.map(Number);
    return [nums.slice(0, 2), nums.slice(-2)];
  }

  const overlaps = (a: DOMRect, b: DOMRect) =>
    a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CareerMapComponent, NoopAnimationsModule],
      providers: [provideRouter([])],
    }).compileComponents();
  });

  afterEach(() => fixture?.destroy());

  it('draws an arrow in from each prerequisite and out to each course that requires it', async () => {
    await render(DECLARED);
    hover('C201');

    expect(el.querySelectorAll('.course-edge').length).toBe(2);
    expect(chip('C101').classList).toContain('is-prereq');
    expect(chip('C301').classList).toContain('is-dependent');
    expect(chip('C202').classList).not.toContain('is-prereq');
    expect(chip('C202').classList).not.toContain('is-dependent');
  });

  it('points every arrow forward in time, prerequisite to dependent', async () => {
    await render(DECLARED);
    hover('C201');

    // Terms stack top to bottom, so forward in time is downward on the map.
    for (const path of Array.from(el.querySelectorAll('.course-edge'))) {
      const [start, end] = ends(path.getAttribute('d')!);
      expect(end[1]).toBeGreaterThan(start[1]);
    }
  });

  it('describes the course in a card under the arrows, not a tooltip over them', async () => {
    await render(DECLARED);
    hover('C201');

    const card = el.querySelector<HTMLElement>('#tn-course-card')!;
    expect(card.textContent).toContain('C201 — Course C201');
    expect(card.textContent).toContain('Prerequisites');
    expect(card.textContent).toContain('C101');
    expect(card.textContent).toContain('Required by');
    expect(card.textContent).toContain('C301');
    expect(chip('C201').getAttribute('aria-describedby')).toBe('tn-course-card');
    expect(el.querySelector('.c-chip.mat-mdc-tooltip-trigger')).toBeNull();

    const z = (sel: string) => Number(getComputedStyle(el.querySelector(sel)!).zIndex);
    expect(z('.course-edges')).toBeGreaterThan(z('.course-card'));
  });

  it('keeps the card off the chips the arrows join', async () => {
    await render(DECLARED);
    hover('C201');

    const card = el.querySelector<HTMLElement>('#tn-course-card')!.getBoundingClientRect();
    for (const code of ['C101', 'C201', 'C301']) {
      expect(overlaps(card, chip(code).getBoundingClientRect()))
        .withContext(code)
        .toBeFalse();
    }
  });

  it('says so in the key when no course declares a prerequisite', async () => {
    await render([]);
    expect(el.querySelector('.legend')!.textContent).toContain('No course declares a prerequisite yet');

    hover('C202');
    expect(el.querySelectorAll('.course-edge').length).toBe(0);
    expect(el.querySelector('#tn-course-card')!.textContent).toContain('none declared');
  });

  it('drops the note once a prerequisite is declared', async () => {
    await render(DECLARED);
    expect(el.querySelector('.legend')!.textContent).not.toContain('No course declares');
  });

  it('clears the arrows and the card when the pointer leaves', async () => {
    await render(DECLARED);
    hover('C201');
    chip('C201').dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: el }));
    fixture.detectChanges();

    expect(el.querySelectorAll('.course-edge').length).toBe(0);
    expect(el.querySelector('#tn-course-card')).toBeNull();
  });
});
