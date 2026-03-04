import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { of, throwError, delay } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { DashboardComponent } from './dashboard.component';
import { ApiService } from '@core/services/api.service';
import { Range, Exercise, HealthResponse } from '@core/models';

describe('DashboardComponent', () => {
  let component: DashboardComponent;
  let fixture: ComponentFixture<DashboardComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;

  const mockHealth: HealthResponse = {
    status: 'ok',
    version: '0.1.0',
    app: 'truenorth',
    db: true,
    redis: true,
  };

  const mockRanges: Partial<Range>[] = [
    { id: 'r1', name: 'Range A', state: 'ready' },
    { id: 'r2', name: 'Range B', state: 'provisioning' },
    { id: 'r3', name: 'Range C', state: 'destroyed' },
  ];

  const mockExercises: Partial<Exercise>[] = [
    { id: 'e1', name: 'Ex 1', state: 'running', total_score: 75, max_score: 100 },
    { id: 'e2', name: 'Ex 2', state: 'completed', total_score: 90, max_score: 100 },
  ];

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService', [
      'health',
      'listRanges',
      'listExercises',
    ]);
    mockApi.health.and.returnValue(of(mockHealth));
    mockApi.listRanges.and.returnValue(of(mockRanges as Range[]));
    mockApi.listExercises.and.returnValue(of(mockExercises as Exercise[]));

    await TestBed.configureTestingModule({
      imports: [
        DashboardComponent,
        NoopAnimationsModule,
        RouterTestingModule,
      ],
      providers: [{ provide: ApiService, useValue: mockApi }],
    }).compileComponents();

    fixture = TestBed.createComponent(DashboardComponent);
    component = fixture.componentInstance;
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should create', () => {
    expect(component).toBeTruthy();
  });

  // ── Loads range stats on init ────────────────────────────────────
  it('should load range stats on init', () => {
    fixture.detectChanges();  // triggers ngOnInit

    expect(mockApi.health).toHaveBeenCalled();
    expect(mockApi.listRanges).toHaveBeenCalledWith(5);
    expect(mockApi.listExercises).toHaveBeenCalledWith(5);
  });

  // ── Displays range count ─────────────────────────────────────────
  it('should display active range count (provisioning + ready + running)', () => {
    fixture.detectChanges();

    // 'ready' and 'provisioning' are active, 'destroyed' is not
    expect(component.rangeCount()).toBe(2);
  });

  // ── Displays exercise count ──────────────────────────────────────
  it('should display exercise count', () => {
    fixture.detectChanges();

    expect(component.exerciseCount()).toBe(2);
  });

  // ── Handles loading state ────────────────────────────────────────
  it('should show initial empty state before data loads', () => {
    // Before ngOnInit
    expect(component.ranges()).toEqual([]);
    expect(component.exercises()).toEqual([]);
    expect(component.health()).toBeNull();
    expect(component.rangeCount()).toBe(0);
    expect(component.exerciseCount()).toBe(0);
  });

  // ── Handles error state ──────────────────────────────────────────
  it('should handle API errors gracefully', () => {
    mockApi.listRanges.and.returnValue(throwError(() => new Error('Network error')));
    mockApi.listExercises.and.returnValue(throwError(() => new Error('Network error')));
    mockApi.health.and.returnValue(throwError(() => new Error('Network error')));

    // Should not throw
    expect(() => fixture.detectChanges()).not.toThrow();

    // State remains at defaults
    expect(component.ranges()).toEqual([]);
    expect(component.exerciseCount()).toBe(0);
  });

  // ── Health display ─────────────────────────────────────────────
  it('should set health data from API', () => {
    fixture.detectChanges();

    expect(component.health()?.status).toBe('ok');
    expect(component.health()?.version).toBe('0.1.0');
  });

  // ── healthTooltip ──────────────────────────────────────────────
  it('healthTooltip() should format DB and Redis status', () => {
    fixture.detectChanges();

    const tip = component.healthTooltip();
    expect(tip).toContain('DB: connected');
    expect(tip).toContain('Redis: connected');
  });

  it('healthTooltip() should return empty string when health is null', () => {
    expect(component.healthTooltip()).toBe('');
  });

  // ── Rendered output ────────────────────────────────────────────
  it('should render stat cards in the template', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const statValues = el.querySelectorAll('.stat-value');
    expect(statValues.length).toBeGreaterThanOrEqual(2);
  });
});