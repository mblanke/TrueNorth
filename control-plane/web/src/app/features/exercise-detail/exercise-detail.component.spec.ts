import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { InjectRecord } from '@core/models';
import { ExerciseDetailComponent } from './exercise-detail.component';

describe('ExerciseDetailComponent detections', () => {
  let fixture: ComponentFixture<ExerciseDetailComponent>;
  let api: jasmine.SpyObj<ApiService>;

  const detail = (state: string) => ({
    exercise_id: 'ex1', exercise_name: 'Hunt', state, total_score: 0, max_score: 20, range_id: '',
    scenario_name: 's', po_id: 'PO 1', environment: 'mock', duration_min: 60, timeline: [], noise_floor: [],
    objectives: [
      { ref_id: 'DET-1', type: 'detection', points: 10, achieved: false, evidence: '', validator: 'validate.opensearch_query', competency_code: '' },
      { ref_id: 'MAN-1', type: 'manual', points: 10, achieved: false, evidence: '', validator: 'manual_ack', competency_code: '' },
    ],
  });
  const attempts: Detection[] = [{
    id: 'd1', objective_ref: 'DET-1', user_id: 'u-1', query: 'q', submitted_at: null,
    verdict: 'missed', events_matched: 4, on_target: 1, precision: 0.25,
  }];

  async function render(role: string, state = 'running'): Promise<HTMLElement> {
    api = jasmine.createSpyObj('ApiService', [
      'get', 'listDetections', 'submitDetection', 'getRange', 'getRangeDiagram', 'listInjects',
    ]);
    api.get.and.returnValue(of(detail(state)) as any);
    api.listDetections.and.returnValue(of(attempts));
    api.listInjects.and.returnValue(of([]));
    const auth = {
      canSubmitDetections: signal(role !== 'observer'),
      isInstructor: signal(role === 'admin' || role === 'instructor'),
    };
    await TestBed.configureTestingModule({
      imports: [ExerciseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 'ex1' }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ExerciseDetailComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  afterEach(() => fixture?.destroy()); // stops the running-exercise poll

  const submitButtons = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes('Submit detection'));

  it('offers Run / replay to staff only (sweep M3: a replay clears everyone)', async () => {
    const runButton = (el: HTMLElement) =>
      Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Provision & Run'));
    expect(runButton(await render('student', 'completed'))).toBeUndefined();
    fixture.destroy();
    TestBed.resetTestingModule();
    expect(runButton(await render('instructor', 'completed'))).toBeDefined();
  });

  it('offers a Student one detection action, on the detection objective only', async () => {
    const el = await render('student');
    expect(api.listDetections).toHaveBeenCalledWith('ex1');
    expect(el.querySelectorAll('tn-detection-panel').length).toBe(1);
    expect(submitButtons(el).length).toBe(1);
  });

  it('offers no detection action to an observer', async () => {
    const el = await render('observer');
    expect(submitButtons(el).length).toBe(0);
  });

  it('offers no detection action when the exercise is not running', async () => {
    const el = await render('student', 'completed');
    expect(submitButtons(el).length).toBe(0);
  });

  it('links a completed exercise to its AAR on the scoring page, next to Generate AAR', async () => {
    const el = await render('instructor', 'completed');
    const view = Array.from(el.querySelectorAll<HTMLAnchorElement>('a'))
      .find(a => a.textContent?.includes('View AAR'));
    expect(view).toBeDefined();
    expect(view!.getAttribute('href')).toBe('/scoring?exercise=ex1');
    expect(Array.from(el.querySelectorAll('button')).some(b => b.textContent?.includes('Generate AAR'))).toBeTrue();
  });

  it('does not offer View AAR to a Student, who cannot open /scoring', async () => {
    const el = await render('student', 'completed');
    expect(Array.from(el.querySelectorAll('a')).some(a => a.textContent?.includes('View AAR'))).toBeFalse();
  });

  it('gives staff the full attempts table', async () => {
    const el = await render('instructor');
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Attempts (1)'))!.click();
    fixture.detectChanges();
    expect(el.querySelector('td.precision')?.textContent?.trim()).toBe('25%');
  });
});

describe('ExerciseDetailComponent inject log', () => {
  let fixture: ComponentFixture<ExerciseDetailComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;

  const detail = {
    exercise_id: 'ex-1', exercise_name: 'Drill', state: 'completed', total_score: 0, max_score: 10,
    range_id: '', scenario_name: 'drill', po_id: '', environment: '', duration_min: 0,
    timeline: [], noise_floor: null, objectives: [],
  };

  const injects: Partial<InjectRecord>[] = [
    { id: 'i1', source: 'timeline', t: '00:00', action: 'simulated_execution', status: 'fired',
      detail: 'simulated_execution', execution_mode: 'simulated' },
    { id: 'i2', source: 'timeline', t: '00:30', action: 'dns_spike', status: 'skipped',
      detail: 'skipped: mock backend (injector needs range hosts)', execution_mode: null },
  ];

  async function setup(listInjects: ReturnType<ApiService['listInjects']>): Promise<HTMLElement> {
    mockApi = jasmine.createSpyObj('ApiService', ['get', 'listInjects', 'getRange', 'getRangeDiagram', 'listDetections']);
    mockApi.get.and.returnValue(of(detail as any));
    mockApi.listInjects.and.returnValue(listInjects);
    mockApi.listDetections.and.returnValue(of([]));
    await TestBed.configureTestingModule({
      imports: [ExerciseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: { canSubmitDetections: signal(false), isInstructor: signal(true) } },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 'ex-1' }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ExerciseDetailComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('lists each recorded inject with its status', async () => {
    const el = await setup(of(injects as InjectRecord[]));
    expect(mockApi.listInjects).toHaveBeenCalledWith('ex-1');
    const rows = Array.from(el.querySelectorAll('.inj'));
    expect(rows.length).toBe(2);
    expect(rows.map(r => r.getAttribute('data-status'))).toEqual(['fired', 'skipped']);
    expect(rows[0].textContent).toContain('simulated');
    expect(rows[1].textContent).toContain('skipped: mock backend');
  });

  it('says nothing was recorded when the log is empty or unreadable', async () => {
    const el = await setup(throwError(() => new Error('500')));
    expect(el.querySelectorAll('.inj').length).toBe(0);
    expect(el.textContent).toContain('No injects recorded yet.');
  });
});
