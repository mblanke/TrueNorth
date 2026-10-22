import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { AuthService, UserRole } from '@core/services/auth.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { NotificationService } from '@core/services/notification.service';
import { RangeCredentialsApiService } from '@core/services/range-credentials-api.service';
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
      'get', 'listDetections', 'submitDetection', 'getRange', 'getRangeDiagram', 'listInjects', 'resumeExercise',
    ]);
    api.get.and.returnValue(of(detail(state)) as any);
    api.listDetections.and.returnValue(of(attempts));
    api.listInjects.and.returnValue(of([]));
    const auth = fakeAuth(role as UserRole);
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

  // R0-B: a Student reads the AAR of an exercise they took part in (aar:read; the API
  // answers 404 for any other), but never generates one.
  it('offers a Student View AAR but not Generate AAR', async () => {
    const el = await render('student', 'completed');
    expect(Array.from(el.querySelectorAll('a')).some(a => a.textContent?.includes('View AAR'))).toBeTrue();
    expect(Array.from(el.querySelectorAll('button')).some(b => b.textContent?.includes('Generate AAR'))).toBeFalse();
  });

  it('does not offer View AAR to range ops, who hold no aar:read', async () => {
    const el = await render('range_ops', 'completed');
    expect(Array.from(el.querySelectorAll('a')).some(a => a.textContent?.includes('View AAR'))).toBeFalse();
  });

  describe('Resume (POST /exercises/{id}/resume)', () => {
    const resumeButton = (el: HTMLElement) =>
      Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Resume'));

    it('resumes a paused exercise and reloads its state', async () => {
      const el = await render('instructor', 'paused');
      api.resumeExercise.and.returnValue(of({} as any));
      api.get.calls.reset();
      api.get.and.returnValue(of(detail('running')) as any);
      resumeButton(el)!.click();
      fixture.detectChanges();
      expect(api.resumeExercise).toHaveBeenCalledOnceWith('ex1');
      expect(api.get).toHaveBeenCalled();
      expect((fixture.componentInstance as any).detail?.state).toBe('running');
      expect(resumeButton(el)).toBeUndefined();
    });

    it('ignores a second click while the first is in flight', async () => {
      const el = await render('instructor', 'paused');
      const pending = new Subject<any>();
      api.resumeExercise.and.returnValue(pending);
      resumeButton(el)!.click();
      fixture.componentInstance.resume();
      expect(api.resumeExercise).toHaveBeenCalledTimes(1);
      pending.complete();
    });

    it('is not offered to a Student or an observer', async () => {
      expect(resumeButton(await render('student', 'paused'))).toBeUndefined();
      fixture.destroy();
      TestBed.resetTestingModule();
      expect(resumeButton(await render('observer', 'paused'))).toBeUndefined();
    });
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
        { provide: AuthService, useValue: fakeAuth('instructor') },
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

describe('ExerciseDetailComponent range accounts (ADR 0012)', () => {
  let fixture: ComponentFixture<ExerciseDetailComponent>;
  let creds: jasmine.SpyObj<RangeCredentialsApiService>;

  async function render(role: UserRole): Promise<HTMLElement> {
    const api = jasmine.createSpyObj('ApiService', [
      'get', 'listInjects', 'getRange', 'getRangeDiagram', 'listDetections', 'listRangeDocuments']);
    api.listRangeDocuments.and.returnValue(of([]));
    api.get.and.returnValue(of({
      exercise_id: 'ex-1', exercise_name: 'Hunt', state: 'completed', total_score: 0, max_score: 0, range_id: 'r1',
      scenario_name: 's', po_id: '', environment: '', duration_min: 0, timeline: [], noise_floor: null, objectives: [],
    } as any));
    api.getRange.and.returnValue(of({ id: 'r1', name: 'SOC range', state: 'ready', description: '' } as any));
    api.getRangeDiagram.and.returnValue(of({ range_id: 'r1', diagram_json: null }));
    api.listInjects.and.returnValue(of([]));
    api.listDetections.and.returnValue(of([]));
    creds = jasmine.createSpyObj('RangeCredentialsApiService', ['list', 'reveal', 'rotate']);
    creds.list.and.returnValue(of({ range_id: 'r1', scope: 'student', accounts: [
      { id: 'c-an', machine: 'jump01', username: 'analyst', role: 'user', student_visible: true,
        generated: true, applied: true, source: 'declared', scenario: false, setup_status: 'applied',
        rotated_at: null, notes: 'The way in' }] }));
    await TestBed.configureTestingModule({
      imports: [ExerciseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: RangeCredentialsApiService, useValue: creds },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: fakeAuth(role) },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 'ex-1' }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ExerciseDetailComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  afterEach(() => fixture?.destroy());

  it("offers a Student their own accounts for the exercise's range, on request", async () => {
    const el = await render('student');
    expect(el.textContent).toContain('Your accounts');
    expect(creds.list).not.toHaveBeenCalled();
    const show = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Show accounts'))!;
    show.click();
    fixture.detectChanges();
    expect(creds.list).toHaveBeenCalledOnceWith({ kind: 'range', rangeId: 'r1' });
    expect(el.textContent).toContain('analyst');
    expect(creds.reveal).not.toHaveBeenCalled();
  });

  it('shows observers no accounts panel', async () => {
    const el = await render('observer');
    expect(el.textContent).not.toContain('Your accounts');
    expect(el.textContent).not.toContain('Show accounts');
  });
});

// Issue #201: what an instructor saw on a real render of a running exercise.
describe('ExerciseDetailComponent instructor polish (#201)', () => {
  let fixture: ComponentFixture<ExerciseDetailComponent>;

  const base = {
    exercise_id: 'ex-1', exercise_name: 'Hunt', state: 'running', total_score: 0, max_score: 30, range_id: '',
    scenario_name: 's', po_id: '', environment: '', duration_min: 0, timeline: [], noise_floor: null,
    objectives: [
      { ref_id: 'detect_c2', description: 'Detect the C2 beacon', type: 'detection', points: 10, achieved: false,
        evidence: '', validator: 'opensearch_query', competency_code: '' },
      { ref_id: 'contain_ws07', description: 'contain_ws07', type: 'response', points: 10, achieved: false,
        evidence: '', validator: 'manual_ack', competency_code: '' },
      { ref_id: 'write_report', description: '', type: 'deliverable', points: 10, achieved: false,
        evidence: '', validator: 'deliverable_check', competency_code: '' },
    ],
  };

  async function render(role: UserRole, patch: Record<string, unknown> = {}): Promise<HTMLElement> {
    const api = jasmine.createSpyObj('ApiService', ['get', 'listInjects', 'getRange', 'getRangeDiagram', 'listDetections']);
    api.get.and.returnValue(of({ ...base, ...patch } as any));
    api.listInjects.and.returnValue(of([]));
    api.listDetections.and.returnValue(of([]));
    await TestBed.configureTestingModule({
      imports: [ExerciseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: fakeAuth(role) },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 'ex-1' }) } } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ExerciseDetailComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  afterEach(() => fixture?.destroy());

  it('takes staff back to Exercises, and a Student back to their career path', async () => {
    let back = (await render('instructor')).querySelector<HTMLAnchorElement>('a.back')!;
    expect(back.textContent).toContain('Exercises');
    expect(back.getAttribute('href')).toBe('/exercises');
    fixture.destroy();
    TestBed.resetTestingModule();
    back = (await render('student')).querySelector<HTMLAnchorElement>('a.back')!;
    expect(back.textContent).toContain('Career path');
    expect(back.getAttribute('href')).toBe('/learning/career-path');
  });

  it('shows a running exercise as live, not as a greyed-out Running… button', async () => {
    const el = await render('instructor');
    const buttons = Array.from(el.querySelectorAll('button'));
    expect(buttons.some(b => b.textContent?.includes('Running'))).toBeFalse();
    expect(buttons.some(b => b.textContent?.includes('Provision & Run'))).toBeFalse();
    expect(el.querySelector('[data-testid="live"]')?.textContent).toContain('Live');
    const ops = Array.from(el.querySelectorAll<HTMLAnchorElement>('a')).find(a => a.textContent?.includes('Ops Center'));
    expect(ops?.getAttribute('href')).toBe('/ops-center/ex-1');
  });

  it('leaves out empty metadata instead of printing "· — ·"', async () => {
    let meta = (await render('instructor')).querySelector('.meta')!.textContent!;
    expect(meta).not.toContain('·');
    expect(meta).not.toContain('—');
    fixture.destroy();
    TestBed.resetTestingModule();
    meta = (await render('instructor', { environment: 'enterprise', duration_min: 90, po_id: 'PO 401' }))
      .querySelector('.meta')!.textContent!;
    expect(meta).toContain('enterprise · 1h 30m · PO 401');
  });

  it('names objectives and labels instructor-judged ones as sign-off', async () => {
    const el = await render('instructor');
    const rows = Array.from(el.querySelectorAll('.obj')).map(r => r.textContent ?? '');
    expect(rows[0]).toContain('Detect the C2 beacon');
    expect(rows[0]).toContain('Detection');
    expect(rows[1]).toContain('Contain WS07');
    expect(rows[1]).toContain('Instructor sign-off');
    expect(rows[1]).not.toContain('contain_ws07');
    expect(rows[2]).toContain('Write report');
    expect(rows[2]).toContain('instructor sign-off');
    expect(rows[2]).not.toMatch(/\bdeliverable\b/);
  });

  it('pads the page like every other page', async () => {
    const el = await render('instructor');
    expect(el.querySelector('.xd')?.classList).toContain('page-container');
  });
});
