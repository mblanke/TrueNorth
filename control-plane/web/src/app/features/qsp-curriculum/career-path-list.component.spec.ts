import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';

import { CareerPathListComponent } from './career-path-list.component';
import { CurriculumMap, PO, QualNode } from '@core/services/curriculum-map.service';

function po(code: string, delivered: boolean): PO {
  return {
    id: code, po_code: code, title: `Objective ${code}`, tier: 'core', environment: '', status: '',
    duration_min: 60, critical_events: [], target_role: '', nice_dcwf_task: '', scenario_count: 0,
    conditions: '', assessment_type: '', pass_standard: '', deliverable: '', build_hours: 0,
    enabling_objectives: [], competencies: [], exercise_id: null, scenario_id: null, range_id: null,
    course_code: null, duration_long: false, progress_state: 'not_started',
    delivered_by: delivered
      ? {
          course_id: 'c1', course_code: 'CYB101', course_name: 'Intro', module_id: 'm1',
          module_title: 'Module 1', dp_order: 1, institution: 'Algonquin', term_code: 'T1',
          is_placeholder: false,
        }
      : null,
  };
}

function node(code: string, dp: number, track: string, state: QualNode['state'], objectives: PO[]): QualNode {
  return {
    qsp_code: code, nqual: code, title: `Qualification ${code}`, dp_order: dp, track, track_key: track,
    rank_level: dp === 1 ? 'Pte' : 'Cpl', po_count: objectives.length, gate_count: 0, total_minutes: 60,
    timed_po_count: objectives.length, course_hours: 0, state,
    progress: { completed: state === 'complete' ? objectives.length : 0, in_progress: 0, total: objectives.length, pct: 0 },
    objectives, courses: [], course_count: 0,
  };
}

const MAP: CurriculumMap = {
  stages: [
    { dp_order: 2, rank_level: 'Cpl', label: 'Operator', planned: false },
    { dp_order: 1, rank_level: 'Pte', label: 'Foundations', planned: false },
    { dp_order: 3, rank_level: 'Sgt', label: 'Team lead', planned: true },
  ],
  tracks: [
    { key: 'ladder', label: 'Rank ladder', kind: 'progression' },
    { key: 'red', label: 'Red analyst', kind: 'specialty' },
  ],
  nodes: [
    node('SPEC1', 1, 'red', 'available', [po('R1', true)]),
    node('TEMP67', 1, 'ladder', 'complete', [po('A1', true)]),
    node('OPS2', 2, 'ladder', 'in_progress', [po('B1', true), po('B2', false), po('B3', false)]),
  ],
  edges: [],
  course_edges: [],
  learner: { current_qsp_code: 'OPS2', current_po_code: null },
};

describe('CareerPathListComponent', () => {
  let fixture: ComponentFixture<CareerPathListComponent>;
  let component: CareerPathListComponent;
  let el: HTMLElement;

  function render(selected: string | null = null): void {
    fixture = TestBed.createComponent(CareerPathListComponent);
    component = fixture.componentInstance;
    component.map = MAP;
    component.selected = selected;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const rowOrder = () =>
    Array.from(el.querySelectorAll<HTMLElement>('[data-qual], [data-planned]')).map(
      r => r.dataset['qual'] ?? `planned:${r.dataset['planned']}`,
    );

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CareerPathListComponent, NoopAnimationsModule],
      providers: [provideRouter([])],
    }).compileComponents();
  });

  it('orders by period, rank ladder before specialty streams', () => {
    render();
    expect(rowOrder()).toEqual(['TEMP67', 'SPEC1', 'OPS2', 'planned:3']);
  });

  it('shows a planned period as a row with nothing to open', () => {
    render();
    const planned = el.querySelector('[data-planned="3"]')!;
    expect(planned.tagName.toLowerCase()).not.toBe('mat-expansion-panel');
    expect(planned.textContent).toContain('planned');
  });

  it('starts with everything closed unless a stage is selected', () => {
    render();
    expect(el.querySelectorAll('mat-expansion-panel.mat-expanded').length).toBe(0);
  });

  it('opens the selected stage (the page selects the current one on load)', () => {
    render('OPS2');
    const open = el.querySelectorAll('mat-expansion-panel.mat-expanded');
    expect(open.length).toBe(1);
    expect((open[0] as HTMLElement).dataset['qual']).toBe('OPS2');
  });

  it('marks where the learner is', () => {
    render();
    const here = el.querySelector('[data-qual="OPS2"]')!;
    expect(here.classList).toContain('here');
    expect(here.textContent).toContain('you are here');
  });

  it('keeps a gap visible on the closed row', () => {
    render();
    const row = el.querySelector('[data-qual="OPS2"]')!;
    expect(row.classList).not.toContain('mat-expanded');
    expect(row.querySelector('.tn-gap-count')?.textContent).toContain('2 not yet delivered');
    expect(el.querySelector('[data-qual="TEMP67"] .tn-gap-count')).toBeNull();
  });

  it('keeps QSP codes off the face of the row', () => {
    render();
    const title = el.querySelector('[data-qual="TEMP67"] .title')!;
    expect(title.textContent?.trim()).toBe('Qualification TEMP67');
    expect(el.querySelector('[data-qual="TEMP67"] mat-expansion-panel-header')!.textContent).not.toContain(
      'TEMP67 ',
    );
  });

  it('labels only specialty streams with their track', () => {
    render();
    expect(el.querySelector('[data-qual="SPEC1"] .track')?.textContent).toContain('Red analyst');
    expect(el.querySelector('[data-qual="TEMP67"] .track')).toBeNull();
  });

  it('emits the opened stage, and null when it is closed again', () => {
    render();
    const emitted: (string | null)[] = [];
    component.selectedChange.subscribe(v => emitted.push(v));

    const header = () =>
      el.querySelector<HTMLElement>('[data-qual="TEMP67"] mat-expansion-panel-header')!;
    header().click();
    fixture.detectChanges();
    header().click();
    fixture.detectChanges();

    expect(emitted).toEqual(['TEMP67', null]);
  });

  it('sums progress across the path for the summary line', () => {
    render();
    // TEMP67 complete (1/1); SPEC1 0/1; OPS2 0/3 -> 1 of 5.
    expect(el.querySelector('.overall')?.textContent).toContain('1 of 5 objectives complete');
    expect(el.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('20');
  });
});
