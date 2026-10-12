import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { EXERCISE_POLL_MS, ExercisesComponent } from './exercises.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { AuthService } from '@core/services/auth.service';
import { Exercise, Range, Scenario } from '@core/models';
import { fakeAuth } from '@core/auth/fake-auth';
import { Subject } from 'rxjs';

describe('ExercisesComponent', () => {
  let component: ExercisesComponent;
  let fixture: ComponentFixture<ExercisesComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;
  let auth: ReturnType<typeof fakeAuth>;

  const mockExercises: Partial<Exercise>[] = [
    { id: 'e1', name: 'IR Drill', state: 'pending', total_score: 0, max_score: 100, created_at: '2026-02-01T10:00:00Z' },
    { id: 'e2', name: 'Red Team', state: 'running', total_score: 55, max_score: 100, created_at: '2026-02-02T10:00:00Z' },
    { id: 'e3', name: 'APT Hunt', state: 'completed', total_score: 92, max_score: 100, created_at: '2026-02-03T10:00:00Z' },
  ];

  const mockRanges: Partial<Range>[] = [
    { id: 'r1', name: 'Range A', state: 'ready' },
  ];

  const mockScenarios: Partial<Scenario>[] = [
    { id: 's1', name: 'APT29 Simulation' },
    { id: 's2', name: 'Ransomware Response' },
  ];

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService', [
      'listExercises',
      'listRanges',
      'listScenarios',
      'createExercise',
      'startExercise',
      'pauseExercise',
      'completeExercise',
      'resumeExercise',
      'generateAAR',
    ]);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error']);

    mockApi.listExercises.and.returnValue(of(mockExercises as Exercise[]));
    mockApi.listRanges.and.returnValue(of(mockRanges as Range[]));
    mockApi.listScenarios.and.returnValue(of(mockScenarios as Scenario[]));
    mockApi.createExercise.and.returnValue(of({ id: 'e-new', name: 'New', state: 'pending' } as Exercise));
    mockApi.startExercise.and.returnValue(of({ id: 'e1', state: 'running' } as Exercise));
    mockApi.pauseExercise.and.returnValue(of({ id: 'e2', state: 'paused' } as Exercise));
    mockApi.completeExercise.and.returnValue(of({ id: 'e2', state: 'completed' } as Exercise));
    mockApi.resumeExercise.and.returnValue(of({ id: 'e4', state: 'running' } as Exercise));
    mockApi.generateAAR.and.returnValue(of({} as any));
    auth = fakeAuth('instructor');

    await TestBed.configureTestingModule({
      imports: [ExercisesComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
        { provide: AuthService, useValue: auth },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ExercisesComponent);
    component = fixture.componentInstance;
  });

  // ── Live state ───────────────────────────────────────────────────
  // The worker runs and scores an exercise; the page used to show it running at 0/100,
  // with Pause and Complete, long after it had finished (found in a browser journey, PR #38).
  it('refreshes while an exercise is running, until none is', fakeAsync(() => {
    const running = [{ ...mockExercises[1] }] as Exercise[];
    const done = [{ ...mockExercises[1], state: 'completed', total_score: 100 }] as Exercise[];
    mockApi.listExercises.and.returnValues(of(running), of(done), of(done));
    fixture.detectChanges();
    expect(mockApi.listExercises).toHaveBeenCalledTimes(1);
    tick(EXERCISE_POLL_MS);
    expect(mockApi.listExercises).toHaveBeenCalledTimes(2);
    expect(component.exercises()[0].state).toBe('completed');
    expect(component.exercises()[0].total_score).toBe(100);
    expect(component.loading()).toBeFalse();
    tick(EXERCISE_POLL_MS * 3);
    expect(mockApi.listExercises).toHaveBeenCalledTimes(2);
    discardPeriodicTasks();
  }));

  it('does not poll when nothing is running', fakeAsync(() => {
    mockApi.listExercises.and.returnValue(of([mockExercises[2]] as Exercise[]));
    fixture.detectChanges();
    tick(EXERCISE_POLL_MS * 3);
    expect(mockApi.listExercises).toHaveBeenCalledTimes(1);
    discardPeriodicTasks();
  }));

  it('stops polling when the page is left', fakeAsync(() => {
    mockApi.listExercises.and.returnValue(of([mockExercises[1]] as Exercise[]));
    fixture.detectChanges();
    fixture.destroy();
    tick(EXERCISE_POLL_MS * 3);
    expect(mockApi.listExercises).toHaveBeenCalledTimes(1);
  }));

  // ── Creation ─────────────────────────────────────────────────────
  it('should create', () => {
    expect(component).toBeTruthy();
  });

  // ── Loads exercises on init ──────────────────────────────────────
  it('should load exercises on init', () => {
    fixture.detectChanges();

    expect(mockApi.listExercises).toHaveBeenCalled();
    expect(component.exercises().length).toBe(3);
  });

  it('should load ranges and scenarios on init', () => {
    fixture.detectChanges();

    expect(mockApi.listRanges).toHaveBeenCalled();
    expect(mockApi.listScenarios).toHaveBeenCalled();
    expect(component.ranges().length).toBe(1);
    expect(component.scenarios().length).toBe(2);
  });

  // ── Start button calls startExercise ─────────────────────────────
  it('start() should call startExercise and show success', () => {
    fixture.detectChanges();

    component.start('e1');

    expect(mockApi.startExercise).toHaveBeenCalledWith('e1');
    expect(mockNotify.success).toHaveBeenCalledWith('Exercise started');
  });

  it('start() should show error on failure', () => {
    mockApi.startExercise.and.returnValue(
      throwError(() => ({ error: { detail: 'Range not ready' } })),
    );
    fixture.detectChanges();

    component.start('e1');

    expect(mockNotify.error).toHaveBeenCalledWith('Range not ready');
  });

  // ── Displays score correctly ─────────────────────────────────────
  it('should display score in the table', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const cells = el.querySelectorAll('td.mat-mdc-cell');
    const scoreTexts = Array.from(cells).map((c) => c.textContent?.trim());
    // Expect "55/100" or "92/100" somewhere
    expect(scoreTexts.some((t) => t?.includes('55/100') || t?.includes('92/100'))).toBeTrue();
  });

  // ── Complete button calls completeExercise ───────────────────────
  it('complete() should call completeExercise and show success', () => {
    fixture.detectChanges();

    component.complete('e2');

    expect(mockApi.completeExercise).toHaveBeenCalledWith('e2');
    expect(mockNotify.success).toHaveBeenCalledWith('Exercise completed');
  });

  it('complete() should show error on failure', () => {
    mockApi.completeExercise.and.returnValue(throwError(() => new Error('fail')));
    fixture.detectChanges();

    component.complete('e2');

    expect(mockNotify.error).toHaveBeenCalledWith('Complete failed');
  });

  // ── AAR button calls generateAAR ─────────────────────────────────
  it('genAAR() should call generateAAR and show success', () => {
    fixture.detectChanges();

    component.genAAR('e3');

    expect(mockApi.generateAAR).toHaveBeenCalledWith('e3');
    expect(mockNotify.success).toHaveBeenCalledWith('AAR generated');
  });

  it('genAAR() should show error on failure', () => {
    mockApi.generateAAR.and.returnValue(throwError(() => new Error('fail')));
    fixture.detectChanges();

    component.genAAR('e3');

    expect(mockNotify.error).toHaveBeenCalledWith('AAR generation failed');
  });

  // ── Pause ────────────────────────────────────────────────────────
  it('pause() should call pauseExercise', () => {
    fixture.detectChanges();

    component.pause('e2');

    expect(mockApi.pauseExercise).toHaveBeenCalledWith('e2');
    expect(mockNotify.success).toHaveBeenCalledWith('Exercise paused');
  });

  // ── Create exercise ──────────────────────────────────────────────
  it('create() should post exercise and reload', () => {
    fixture.detectChanges();

    component.form = { name: 'New Drill', range_id: 'r1', scenario_id: 's1' };
    component.create();

    expect(mockApi.createExercise).toHaveBeenCalledWith({
      name: 'New Drill',
      range_id: 'r1',
      scenario_id: 's1',
    });
    expect(mockNotify.success).toHaveBeenCalledWith('Exercise created');
  });

  // ── Table columns ────────────────────────────────────────────────
  it('should have the expected columns', () => {
    expect(component.columns).toEqual(['name', 'state', 'score', 'created', 'actions']);
  });

  // ── Rendered rows ────────────────────────────────────────────────
  it('should render exercise rows in the table', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const rows = el.querySelectorAll('tr.mat-mdc-row');
    expect(rows.length).toBe(3);
  });

  it('links each run to its own exercise workspace', () => {
    fixture.detectChanges();
    const links = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('td a'));
    expect(links.some(link => link.getAttribute('href') === '/exercises/e2')).toBeTrue();
  });

  // ── Resume (R0-B) ────────────────────────────────────────────────
  // The Resume button called start(), and POST /start answers 409 unless pending, so a
  // paused exercise could never carry on.
  describe('Resume', () => {
    const paused = { id: 'e4', name: 'Paused Drill', state: 'paused', total_score: 10, max_score: 100,
      created_at: '2026-02-04T10:00:00Z' } as Exercise;
    const running = { ...paused, state: 'running' } as Exercise;
    const button = (label: string) => (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);

    it('POSTs /resume (not /start) and shows the exercise running after reload', () => {
      mockApi.listExercises.and.returnValues(of([paused]), of([running]));
      fixture.detectChanges();
      button('Resume exercise')!.click();
      fixture.detectChanges();
      expect(mockApi.resumeExercise).toHaveBeenCalledOnceWith('e4');
      expect(mockApi.startExercise).not.toHaveBeenCalled();
      expect(mockNotify.success).toHaveBeenCalledWith('Exercise resumed');
      expect(component.exercises()[0].state).toBe('running');
      expect(button('Resume exercise')).toBeNull();
      expect(button('Pause exercise')).not.toBeNull();
    });

    it('sends one request for a double click, and disables the button meanwhile', () => {
      const inFlight = new Subject<Exercise>();
      mockApi.resumeExercise.and.returnValue(inFlight);
      mockApi.listExercises.and.returnValue(of([paused]));
      fixture.detectChanges();
      button('Resume exercise')!.click();
      component.resume('e4');
      fixture.detectChanges();
      expect(mockApi.resumeExercise).toHaveBeenCalledTimes(1);
      expect(button('Resume exercise')!.disabled).toBeTrue();
      inFlight.next(running);
      inFlight.complete();
      fixture.detectChanges();
      expect(component.isBusy('e4')).toBeFalse();
    });

    it('shows the server\'s reason when the exercise is no longer paused, and reloads', () => {
      mockApi.resumeExercise.and.returnValue(
        throwError(() => ({ status: 409, error: { detail: 'Exercise is completed, expected paused' } })),
      );
      mockApi.listExercises.and.returnValues(of([paused]), of([{ ...paused, state: 'completed' } as Exercise]));
      fixture.detectChanges();
      component.resume('e4');
      fixture.detectChanges();
      expect(mockNotify.error).toHaveBeenCalledWith('Exercise is completed, expected paused');
      expect(component.exercises()[0].state).toBe('completed');
      expect(component.isBusy('e4')).toBeFalse();
    });
  });

  // ── Role-aware controls (rbac.py) ────────────────────────────────
  describe('as a Student', () => {
    beforeEach(() => auth.role.set('student'));

    it('shows no lifecycle, create or Generate AAR controls, but keeps the AAR download', () => {
      mockApi.listExercises.and.returnValue(of([
        ...(mockExercises as Exercise[]),
        { id: 'e4', name: 'P', state: 'paused', total_score: 0, max_score: 1, created_at: '' } as Exercise,
      ]));
      fixture.detectChanges();
      const labels = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'))
        .map(b => b.getAttribute('aria-label') ?? b.textContent!.trim());
      for (const forbidden of ['Start exercise', 'Pause exercise', 'Resume exercise', 'Complete exercise',
        'Rename exercise', 'Generate after-action report']) {
        expect(labels).not.toContain(forbidden);
      }
      expect(labels.some(l => /New Exercise/.test(l))).toBeFalse();
      expect(labels).toContain('Download after-action report PDF');
      expect(mockApi.listRanges).not.toHaveBeenCalled();
    });

    it('does not offer to create one when the list is empty', () => {
      mockApi.listExercises.and.returnValue(of([]));
      fixture.detectChanges();
      expect((fixture.nativeElement as HTMLElement).textContent).toContain('Your instructor sets exercises up');
    });
  });

  it('gives an observer read-only rows', () => {
    auth.role.set('observer');
    fixture.detectChanges();
    const labels = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'))
      .map(b => b.getAttribute('aria-label'));
    expect(labels).not.toContain('Pause exercise');
    expect(labels).not.toContain('Start exercise');
  });

  it('shows a retry state when the exercise request fails', () => {
    mockApi.listExercises.and.returnValue(throwError(() => new Error('offline')));
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Exercises could not be loaded');
    expect(el.textContent).not.toContain('No exercises yet');
  });
});
