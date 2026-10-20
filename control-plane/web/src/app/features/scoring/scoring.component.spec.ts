import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { fakeAuth } from '@core/auth/fake-auth';
import { of, throwError } from 'rxjs';

import { ScoringComponent } from './scoring.component';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { AAR, Exercise, Objective } from '@core/models';

const exercise: Exercise = {
  id: 'ex1', name: 'Blue Team Day 1', range_id: 'r1', scenario_id: 's1', state: 'running',
  total_score: 25, max_score: 100, created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z',
};

const objectives: Objective[] = [
  {
    id: 'o1', exercise_id: 'ex1', ref_id: 'obj-a', objective_type: 'detection',
    description: 'Detect C2 callback', validator: 'manual', points: 60, achieved: false,
  },
  {
    id: 'o2', exercise_id: 'ex1', ref_id: 'obj-b', objective_type: 'detection',
    description: 'Contain host', validator: 'manual', points: 25, achieved: true,
    achieved_at: '2026-10-06T01:00:00Z', evidence: 'Acknowledged by Sgt Rivera',
  },
];

/** Objective acknowledgement is objective:ack — instructors and admins only (ADR 0005 §4). */
describe('ScoringComponent acknowledge control', () => {
  let fixture: ComponentFixture<ScoringComponent>;

  function render(canAck: boolean): HTMLElement {
    const api = jasmine.createSpyObj('ApiService', [
      'listExercises', 'getExercise', 'listObjectives', 'getAAR', 'ackObjective', 'generateAAR', 'getAARHtml',
      'getAARPdf',
    ]);
    api.listExercises.and.returnValue(of([]));
    api.getExercise.and.returnValue(of(exercise));
    api.listObjectives.and.returnValue(of(objectives));
    api.getAAR.and.returnValue(throwError(() => new Error('none')));
    api.getAARHtml.and.returnValue(throwError(() => ({ status: 404 })));

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ScoringComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: fakeAuth(canAck ? 'instructor' : 'student') },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({}) } } },
      ],
    });
    fixture = TestBed.createComponent(ScoringComponent);
    fixture.componentInstance.selectedExerciseId = 'ex1';
    fixture.componentInstance.loadExercise();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const ackButtons = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes('Acknowledge'));

  it('hides Acknowledge from a Student, who still sees the objectives', () => {
    const el = render(false);
    expect(el.textContent).toContain('Detect C2 callback');
    expect(ackButtons(el).length).toBe(0);
  });

  it('shows Acknowledge to an instructor on unachieved objectives only', () => {
    const el = render(true);
    expect(ackButtons(el).length).toBe(1);
  });
});

describe('ScoringComponent (after-action review)', () => {
  let fixture: ComponentFixture<ScoringComponent>;
  let component: ScoringComponent;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let query: Record<string, string>;
  let auth: ReturnType<typeof fakeAuth>;

  const exercise = { id: 'e1', name: 'Op Tern', state: 'completed', total_score: 60, max_score: 100 } as Exercise;
  const aar = { id: 'a1', exercise_id: 'e1', report_json: '{}', report_html: '' } as unknown as AAR;
  const page = '<!doctype html><html><body><h1>After-Action Report: Op Tern</h1></body></html>';

  async function create(): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [ScoringComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: auth },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(query) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ScoringComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  beforeEach(() => {
    query = {};
    auth = fakeAuth('instructor');
    api = jasmine.createSpyObj('ApiService', [
      'listExercises', 'getExercise', 'listObjectives', 'getAAR', 'generateAAR', 'getAARHtml', 'getAARPdf', 'ackObjective',
    ]);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);
    api.listExercises.and.returnValue(of([exercise as any]));
    api.getExercise.and.returnValue(of(exercise));
    api.listObjectives.and.returnValue(of([]));
    api.getAAR.and.returnValue(of(aar));
    api.generateAAR.and.returnValue(of(aar));
    api.getAARHtml.and.returnValue(of(page));
    api.getAARPdf.and.returnValue(of(new Blob(['%PDF-1.4'], { type: 'application/pdf' })));
  });

  it('opens on ?exercise= and shows the stored report in a sandboxed iframe', async () => {
    query = { exercise: 'e1' };
    await create();
    expect(api.getExercise).toHaveBeenCalledWith('e1');
    expect(api.getAARHtml).toHaveBeenCalledWith('e1');
    fixture.detectChanges();
    const frame: HTMLIFrameElement = fixture.nativeElement.querySelector('iframe.aar-frame');
    expect(frame).toBeTruthy();
    expect(frame.getAttribute('sandbox')).toBe('');
    expect(frame.getAttribute('srcdoc')).toContain('After-Action Report: Op Tern');
  });

  it('fetches the HTML through the API client instead of opening an unauthenticated URL', async () => {
    await create();
    const open = spyOn(window, 'open');
    component.selectedExerciseId = 'e1';
    component.viewAARHtml();
    expect(api.getAARHtml).toHaveBeenCalledWith('e1');
    expect(open).not.toHaveBeenCalled();
    expect(component.aarHtml()).not.toBeNull();
  });

  it('generating the AAR loads its page', async () => {
    await create();
    component.selectedExerciseId = 'e1';
    component.generateAAR();
    expect(api.generateAAR).toHaveBeenCalledWith('e1');
    expect(notify.success).toHaveBeenCalledWith('AAR generated');
    expect(api.getAARHtml).toHaveBeenCalledWith('e1');
  });

  it('downloads the PDF through the API client', async () => {
    await create();
    const createUrl = spyOn(window.URL, 'createObjectURL').and.returnValue('blob:aar');
    spyOn(window.URL, 'revokeObjectURL');
    const click = spyOn(HTMLAnchorElement.prototype, 'click');
    component.selectedExerciseId = 'e1';
    component.downloadAARPdf();
    expect(api.getAARPdf).toHaveBeenCalledWith('e1');
    expect(createUrl).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
  });

  it('reports a missing AAR instead of showing a frame', async () => {
    api.getAARHtml.and.returnValue(throwError(() => ({ status: 404, error: { detail: 'AAR not found — generate it first' } })));
    await create();
    component.selectedExerciseId = 'e1';
    component.viewAARHtml();
    expect(notify.error).toHaveBeenCalledWith('AAR not found — generate it first');
    expect(component.aarHtml()).toBeNull();
  });

  it('reports a failed PDF download', async () => {
    api.getAARPdf.and.returnValue(throwError(() => ({ status: 404 })));
    await create();
    component.selectedExerciseId = 'e1';
    component.downloadAARPdf();
    expect(notify.error).toHaveBeenCalledWith('PDF download failed. Generate the AAR first.');
  });

  it('shows View report and Download PDF once an AAR exists', async () => {
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-view"]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-pdf"]')).toBeTruthy();
  });

  // R0-B: observers read Scoring & AAR without its mutating controls; a Student reads the
  // report of an exercise they took part in and is told plainly when there is none.
  const generateButton = () => Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLElement>)
    .find(b => /Generate AAR|Regenerate AAR/.test(b.textContent ?? ''));

  it('gives an observer the report but no Generate or Acknowledge', async () => {
    auth = fakeAuth('observer');
    api.listObjectives.and.returnValue(of([{ id: 'o1', exercise_id: 'e1', ref_id: 'x', objective_type: 'detection',
      description: 'd', validator: 'manual', points: 5, achieved: false } as Objective]));
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    expect(generateButton()).toBeUndefined();
    expect(fixture.nativeElement.textContent).not.toContain('Acknowledge');
    expect(fixture.nativeElement.querySelector('[data-testid="aar-view"]')).toBeTruthy();
  });

  it('tells a Student there is no report they can open (404), instead of offering Generate', async () => {
    auth = fakeAuth('student');
    api.getAAR.and.returnValue(throwError(() => ({ status: 404, error: { detail: 'AAR not found' } })));
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    expect(generateButton()).toBeUndefined();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-none"]')?.textContent)
      .toContain('an exercise you took part in');
    expect(fixture.nativeElement.querySelector('iframe.aar-frame')).toBeNull();
  });

  // Issue #201: on a running exercise the page asked for the AAR and logged a 404.
  it('does not ask for the AAR of a running exercise, and says when it will exist', async () => {
    api.getExercise.and.returnValue(of({ ...exercise, state: 'running' } as Exercise));
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    expect(api.getAAR).not.toHaveBeenCalled();
    expect(api.getAARHtml).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-pending"]')?.textContent)
      .toContain('once the exercise ends');
    expect(generateButton()).toBeUndefined();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-none"]')).toBeNull();
  });

  it('shows who detected what instead of the stored evidence JSON', async () => {
    api.listObjectives.and.returnValue(of([
      { id: 'o1', exercise_id: 'e1', ref_id: 'detect_c2', objective_type: 'detection', description: 'detect_c2',
        validator: 'opensearch_query', points: 40, achieved: true, achieved_at: '2026-10-19T10:00:00Z',
        evidence: JSON.stringify({ source: 'student_detection', submission_id: 's1', student_id: 'u1',
          student: 'Sam Student', events_matched: 3, on_target: 3 }) },
      { id: 'o2', exercise_id: 'e1', ref_id: 'write_report', objective_type: 'deliverable', description: '',
        validator: 'deliverable_check', points: 10, achieved: false },
    ] as Objective[]));
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent;
    expect(text).toContain('Detected by Sam Student: 3 events matched, 3 of the attack');
    expect(text).not.toContain('student_detection');
    expect(text).not.toContain('submission_id');
    const titles = Array.from(fixture.nativeElement.querySelectorAll('mat-card-title') as NodeListOf<HTMLElement>)
      .map(t => t.textContent?.trim());
    expect(titles).toContain('Detect C2');
    expect(titles).toContain('Write report');
    expect(text).toContain('Deliverable · instructor sign-off');
  });

  it('says the report failed to load rather than that there is none', async () => {
    auth = fakeAuth('observer');
    api.getAAR.and.returnValue(throwError(() => ({ status: 500 })));
    query = { exercise: 'e1' };
    await create();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-error"]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('[data-testid="aar-none"]')).toBeNull();
  });
});
