import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ExercisesComponent } from './exercises.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Exercise, Range, Scenario } from '@core/models';

describe('ExercisesComponent', () => {
  let component: ExercisesComponent;
  let fixture: ComponentFixture<ExercisesComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;

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
    mockApi.generateAAR.and.returnValue(of({} as any));

    await TestBed.configureTestingModule({
      imports: [ExercisesComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ExercisesComponent);
    component = fixture.componentInstance;
  });

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
});