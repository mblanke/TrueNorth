import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { CareerMapComponent } from './career-map.component';
import {
  CurriculumMap,
  NodeCourse,
  NodeState,
  QualNode,
} from '@core/services/curriculum-map.service';

function node(
  qsp_code: string,
  dp_order: number,
  track_key: string,
  state: NodeState = 'available',
  overrides: Partial<QualNode> = {},
): QualNode {
  return {
    qsp_code,
    nqual: qsp_code,
    title: `${qsp_code} qualification`,
    dp_order,
    track: track_key === 'progression' ? 'progression' : 'specialty',
    track_key,
    rank_level: 'Cpl',
    po_count: 2,
    gate_count: 0,
    total_minutes: 120,
    timed_po_count: 2,
    state,
    progress: { completed: 0, in_progress: 0, total: 2, pct: 0 },
    objectives: [],
    courses: [],
    course_count: 0,
    course_hours: 0,
    ...overrides,
  };
}

function course(course_id: string, course_code: string, dp: number): NodeCourse {
  return {
    course_id,
    course_code,
    name: `${course_code} course`,
    institution: 'Algonquin College',
    term_code: `DP${dp}-Y1-F`,
    term_label: `DP ${dp} Year 1 Fall`,
    term_start: '2025-09-01',
    duration_hours: 45,
    difficulty: 'intermediate',
    is_published: true,
    delivers: [],
    delivers_cross_dp: false,
  };
}

const MAP: CurriculumMap = {
  stages: [
    { dp_order: 1, rank_level: 'Pte', label: 'Basic occupation', planned: false },
    { dp_order: 2, rank_level: 'Cpl', label: 'Journeyman', planned: false },
    { dp_order: 3, rank_level: 'Sgt', label: 'Supervisor', planned: true },
  ],
  tracks: [
    { key: 'progression', label: 'Core progression', kind: 'progression' },
    { key: 'ALRA-RED', label: 'Red — Adversary Emulation', kind: 'specialty' },
  ],
  nodes: [
    node('ALJQ', 1, 'progression', 'complete', {
      progress: { completed: 2, in_progress: 0, total: 2, pct: 100 },
      courses: [course('c1', 'C101', 1)],
      course_count: 1,
      course_hours: 45,
    }),
    node('TEMP67', 2, 'progression', 'in_progress', {
      gate_count: 1,
      progress: { completed: 1, in_progress: 1, total: 2, pct: 50 },
      courses: [course('c2', 'C201', 2)],
      course_count: 1,
      course_hours: 45,
    }),
    // Only one of four objectives is scoped — much of the real spine looks like this.
    node('TEMP64', 2, 'ALRA-RED', 'locked', {
      po_count: 4,
      timed_po_count: 1,
      total_minutes: 240,
    }),
  ],
  edges: [
    { from: 'ALJQ', to: 'TEMP67', kind: 'progression' },
    { from: 'ALJQ', to: 'TEMP64', kind: 'branch' },
    { from: 'TEMP67', to: 'planned:3', kind: 'planned' },
  ],
  // A cross-bubble follow-up: C101 (in ALJQ's programme) leads into C201 (TEMP67's).
  course_edges: [{ from: 'c1', to: 'c2' }],
  learner: { current_qsp_code: 'TEMP67', current_po_code: 'PO_008' },
};

describe('CareerMapComponent', () => {
  let component: CareerMapComponent;
  let fixture: ComponentFixture<CareerMapComponent>;

  const nodeButtons = (): HTMLButtonElement[] =>
    Array.from(fixture.nativeElement.querySelectorAll('button.node-main'));

  // The bubble div carries data-node and the state/here/selected classes; the
  // button inside it carries the aria attributes and the roving tabindex.
  const bubbleFor = (code: string): HTMLElement =>
    fixture.nativeElement.querySelector(`div.node[data-node="${code}"]`);

  const buttonFor = (code: string): HTMLButtonElement =>
    fixture.nativeElement.querySelector(`div.node[data-node="${code}"] button.node-main`);

  const press = (key: string) => {
    fixture.nativeElement.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    fixture.detectChanges();
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CareerMapComponent, NoopAnimationsModule],
      // The course chips are routerLinks, so the fixture needs a real (empty) router.
      providers: [provideRouter([])],
    }).compileComponents();

    fixture = TestBed.createComponent(CareerMapComponent);
    component = fixture.componentInstance;
    // setInput, not direct assignment — the component is OnPush, so an input has
    // to be set the way a real template binding would set it.
    fixture.componentRef.setInput('map', MAP);
    fixture.detectChanges();
  });

  it('creates', () => {
    expect(component).toBeTruthy();
  });

  describe('placement', () => {
    it('renders one button per qualification', () => {
      expect(nodeButtons().length).toBe(3);
      expect(buttonFor('ALJQ')).toBeTruthy();
      expect(buttonFor('TEMP64')).toBeTruthy();
    });

    it('puts each node in its own lane and DP column', () => {
      const lanes = component['lanes']();
      expect(lanes.length).toBe(2);

      const core = lanes[0].cells;
      expect(core[0].node?.qsp_code).toBe('ALJQ'); // DP1
      expect(core[1].node?.qsp_code).toBe('TEMP67'); // DP2
      expect(core[2].node).toBeNull(); // DP3 is planned

      const red = lanes[1].cells;
      expect(red[0].node).toBeNull(); // no DP1 specialty
      expect(red[1].node?.qsp_code).toBe('TEMP64');
    });

    it('draws a ghost placeholder for a planned DP on the ladder only', () => {
      expect(fixture.nativeElement.querySelector('[data-node="planned:3"]')).toBeTruthy();
      // The specialty lane gets no ghost — only the rank ladder continues.
      expect(fixture.nativeElement.querySelectorAll('.node.ghost').length).toBe(1);
    });

    it('renders a column header per stage, flagging the planned one', () => {
      const heads: HTMLElement[] = Array.from(
        fixture.nativeElement.querySelectorAll('.stage-head'),
      );
      expect(heads.length).toBe(3);
      expect(heads[2].classList).toContain('planned');
      expect(heads[2].textContent).toContain('Not yet defined');
    });
  });

  describe('node presentation', () => {
    it('reflects state as a class so locked and complete read differently', () => {
      expect(bubbleFor('ALJQ').classList).toContain('state-complete');
      expect(bubbleFor('TEMP67').classList).toContain('state-in_progress');
      expect(bubbleFor('TEMP64').classList).toContain('state-locked');
    });

    it('marks the learner position on exactly one node', () => {
      expect(fixture.nativeElement.querySelectorAll('.node.here').length).toBe(1);
      expect(bubbleFor('TEMP67').classList).toContain('here');
      expect(buttonFor('TEMP67').getAttribute('aria-current')).toBe('step');
      expect(buttonFor('ALJQ').getAttribute('aria-current')).toBeNull();
    });

    it('fills the progress ring in proportion to completed objectives', () => {
      const full = component['ringOffset'](MAP.nodes[0]); // 100%
      const half = component['ringOffset'](MAP.nodes[1]); // 50%
      const none = component['ringOffset'](MAP.nodes[2]); // 0%
      expect(full).toBe(0);
      expect(none).toBeGreaterThan(half);
      expect(half).toBeCloseTo(none / 2, 5);
    });

    it('spells the whole node state out for screen readers', () => {
      const label = buttonFor('TEMP67').getAttribute('aria-label') ?? '';
      expect(label).toContain('DP 2, Cpl.');
      expect(label).toContain('TEMP67 — TEMP67 qualification.');
      expect(label).toContain('In progress, 1 of 2 objectives complete.');
      expect(label).toContain('1 gating assessment.');
      expect(label).toContain('Current position.');
    });
  });

  describe('selection', () => {
    it('emits the clicked qualification', () => {
      const emitted: string[] = [];
      component.selectedChange.subscribe(code => emitted.push(code));

      buttonFor('TEMP64').click();
      expect(emitted).toEqual(['TEMP64']);
    });

    it('marks the selected node', () => {
      fixture.componentRef.setInput('selected', 'ALJQ');
      fixture.detectChanges();

      expect(bubbleFor('ALJQ').classList).toContain('selected');
      expect(buttonFor('ALJQ').getAttribute('aria-pressed')).toBe('true');
      expect(buttonFor('TEMP67').getAttribute('aria-pressed')).toBe('false');
    });

    it('still lets a locked node be opened for inspection', () => {
      const emitted: string[] = [];
      component.selectedChange.subscribe(code => emitted.push(code));

      buttonFor('TEMP64').click();
      // Locked means "not yet earnable", not "not viewable" — a learner should be
      // able to read what a future qualification demands.
      expect(emitted).toEqual(['TEMP64']);
      expect(buttonFor('TEMP64').disabled).toBeFalse();
    });
  });

  describe('keyboard navigation', () => {
    it('exposes exactly one node in the tab order', () => {
      expect(nodeButtons().filter(b => b.getAttribute('tabindex') === '0').length).toBe(1);
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('0');
    });

    it('moves the roving tabindex right and left along the DP columns', () => {
      press('ArrowRight');
      expect(buttonFor('TEMP67').getAttribute('tabindex')).toBe('0');
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('-1');

      press('ArrowLeft');
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('0');
    });

    it('crosses lanes with up and down', () => {
      press('ArrowDown');
      expect(buttonFor('TEMP64').getAttribute('tabindex')).toBe('0');

      press('ArrowUp');
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('0');
    });

    it('jumps to the ends with Home and End', () => {
      press('End');
      expect(buttonFor('TEMP64').getAttribute('tabindex')).toBe('0');

      press('Home');
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('0');
    });

    it('does not wrap past either end', () => {
      press('ArrowLeft'); // already at the first node
      expect(buttonFor('ALJQ').getAttribute('tabindex')).toBe('0');

      press('End');
      press('ArrowRight'); // already at the last node
      expect(buttonFor('TEMP64').getAttribute('tabindex')).toBe('0');
    });

    it('skips empty cells rather than trapping focus in them', () => {
      // The Red lane has no DP1 node, so arrowing down from ALJQ lands on TEMP64.
      press('ArrowDown');
      expect(buttonFor('TEMP64').getAttribute('tabindex')).toBe('0');
    });
  });

  describe('prerequisite arrows', () => {
    it('stamps one arrowhead marker per edge state', () => {
      for (const s of ['locked', 'available', 'in_progress', 'complete', 'planned']) {
        expect(fixture.nativeElement.querySelector(`svg.edges marker#edge-arrow-${s}`))
          .withContext(s)
          .toBeTruthy();
      }
    });

    it('points each edge at the arrowhead of its state', () => {
      // measure() normally runs on a rAF outside the zone; call it directly.
      component['measure']();
      fixture.detectChanges();

      const edges: SVGPathElement[] = Array.from(
        fixture.nativeElement.querySelectorAll('svg.edges path.edge'),
      );
      expect(edges.length).toBe(MAP.edges.length);
      for (const edge of edges) {
        const state = edge.getAttribute('data-state');
        expect(edge.getAttribute('marker-end')).toBe(`url(#edge-arrow-${state})`);
      }
    });
  });

  describe('chain highlight', () => {
    const mapEl = (): HTMLElement => fixture.nativeElement.querySelector('.map');

    it('traces the incoming chain on hover, source included', () => {
      bubbleFor('TEMP64').dispatchEvent(new PointerEvent('pointerenter'));
      fixture.detectChanges();

      expect(mapEl().classList).toContain('chain-active');
      expect(bubbleFor('TEMP64').classList).toContain('on-chain'); // never dims itself
      expect(bubbleFor('ALJQ').classList).toContain('on-chain'); // its prerequisite
      expect(bubbleFor('TEMP67').classList).not.toContain('on-chain');

      bubbleFor('TEMP64').dispatchEvent(new PointerEvent('pointerleave'));
      fixture.detectChanges();
      expect(mapEl().classList).not.toContain('chain-active');
    });

    it('traces the chain from keyboard focus too', () => {
      buttonFor('TEMP67').dispatchEvent(new Event('focus'));
      fixture.detectChanges();
      expect(mapEl().classList).toContain('chain-active');
      expect(bubbleFor('ALJQ').classList).toContain('on-chain');

      buttonFor('TEMP67').dispatchEvent(new Event('blur'));
      fixture.detectChanges();
      expect(mapEl().classList).not.toContain('chain-active');
    });
  });

  describe('course follow-up arrows', () => {
    const chip = (id: string): HTMLElement =>
      fixture.nativeElement.querySelector(`a.c-chip[data-course="${id}"]`);
    const arrows = (): NodeListOf<SVGPathElement> =>
      fixture.nativeElement.querySelectorAll('svg.course-edges path.course-edge');

    it('gives each course chip its identity and link', () => {
      expect(chip('c1')).toBeTruthy();
      expect(chip('c1').getAttribute('href')).toContain('/learning/courses/c1');
    });

    it('draws an arrow to the follow-up chip while a chip is hovered', () => {
      chip('c1').dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
      fixture.detectChanges();

      expect(arrows().length).toBe(1);
      expect(arrows()[0].getAttribute('marker-end')).toBe('url(#course-arrow)');
      expect(chip('c2').classList).toContain('follow-target');

      chip('c1').dispatchEvent(
        new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }),
      );
      fixture.detectChanges();
      expect(arrows().length).toBe(0);
      expect(chip('c2').classList).not.toContain('follow-target');
    });

    it('draws the same arrows from keyboard focus', () => {
      chip('c1').dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
      fixture.detectChanges();
      expect(arrows().length).toBe(1);

      chip('c1').dispatchEvent(
        new FocusEvent('focusout', { bubbles: true, relatedTarget: document.body }),
      );
      fixture.detectChanges();
      expect(arrows().length).toBe(0);
    });

    it('draws nothing for a course with no follow-up', () => {
      chip('c2').dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
      fixture.detectChanges();
      expect(arrows().length).toBe(0);
    });
  });

  describe('empty map', () => {
    it('renders nothing rather than throwing', () => {
      fixture.componentRef.setInput('map', {
        stages: [], tracks: [], nodes: [], edges: [], course_edges: [],
        learner: { current_qsp_code: null, current_po_code: null },
      });
      fixture.detectChanges();

      expect(nodeButtons().length).toBe(0);
      expect(component['lanes']()).toEqual([]);
    });
  });
});
