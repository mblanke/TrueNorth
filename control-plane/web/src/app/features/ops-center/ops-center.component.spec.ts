import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute } from '@angular/router';
import { OpsCenterComponent } from './ops-center.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';

describe('OpsCenterComponent', () => {
  let component: OpsCenterComponent;
  let fixture: ComponentFixture<OpsCenterComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;

  const exerciseId = 'ex-001';

  const mockAnnotations = [
    { id: 'a1', user_display_name: 'Alice', content: 'Suspicious DNS traffic', annotation_type: 'finding', severity: 'high', tags: ['dns'], created_at: '2026-04-17T10:00:00Z' },
    { id: 'a2', user_display_name: 'Bob', content: 'Baseline looks normal', annotation_type: 'observation', severity: 'info', tags: [], created_at: '2026-04-17T10:05:00Z' },
  ];

  const mockCommands = [
    { id: 'c1', user_display_name: 'Alice', command: 'Get-WinEvent -LogName Security', description: 'Check security log', host_tag: 'DC01', shared_at: '2026-04-17T10:10:00Z' },
  ];

  const mockStats = {
    exercise_id: exerciseId,
    active_analysts: 4,
    annotations_count: 2,
    shared_commands_count: 1,
    objectives_completed: 2,
    objectives_total: 5,
    elapsed_seconds: 3661,
  };

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService', ['get', 'post']);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error']);

    mockApi.get.and.callFake((path: string): any => {
      if (path.includes('annotations')) return of(mockAnnotations);
      if (path.includes('commands')) return of(mockCommands);
      if (path.includes('stats')) return of(mockStats);
      return of([]);
    });

    await TestBed.configureTestingModule({
      imports: [OpsCenterComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => exerciseId } } },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(OpsCenterComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => {
    // Clear any active intervals
    component.ngOnDestroy();
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should create', () => {
    expect(component).toBeTruthy();
  });

  // ── ngOnInit loads data ──────────────────────────────────────────
  it('should load annotations, commands, and stats on init', () => {
    fixture.detectChanges();

    expect(mockApi.get).toHaveBeenCalledWith(`/ops/exercises/${exerciseId}/annotations`);
    expect(mockApi.get).toHaveBeenCalledWith(`/ops/exercises/${exerciseId}/commands`);
    expect(mockApi.get).toHaveBeenCalledWith(`/ops/exercises/${exerciseId}/stats`);
  });

  it('should set exerciseId from route params', () => {
    fixture.detectChanges();
    expect(component.exerciseId).toBe(exerciseId);
  });

  // ── Annotations ──────────────────────────────────────────────────
  it('should populate annotations signal from API', () => {
    fixture.detectChanges();
    expect(component.annotations().length).toBe(2);
    expect(component.annotations()[0].content).toBe('Suspicious DNS traffic');
  });

  it('should submit an annotation and reload', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of({}));

    component.newAnnotation = 'New finding';
    component.annotationType = 'finding';
    component.annotationSeverity = 'high';
    component.submitAnnotation();

    expect(mockApi.post).toHaveBeenCalledWith(
      `/ops/exercises/${exerciseId}/annotations`,
      jasmine.objectContaining({ content: 'New finding', annotation_type: 'finding', severity: 'high' }),
    );
  });

  it('should not submit empty annotations', () => {
    fixture.detectChanges();
    component.newAnnotation = '   ';
    component.submitAnnotation();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it('should clear annotation input after successful submit', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of({}));
    component.newAnnotation = 'Test note';
    component.submitAnnotation();
    expect(component.newAnnotation).toBe('');
  });

  // ── Shared Commands ──────────────────────────────────────────────
  it('should populate shared commands from API', () => {
    fixture.detectChanges();
    expect(component.sharedCommands().length).toBe(1);
    expect(component.sharedCommands()[0].command).toBe('Get-WinEvent -LogName Security');
  });

  it('should submit a command and reload', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of({}));

    component.newCommand = 'netstat -an';
    component.commandHost = 'WEB01';
    component.submitCommand();

    expect(mockApi.post).toHaveBeenCalledWith(
      `/ops/exercises/${exerciseId}/commands`,
      jasmine.objectContaining({ command: 'netstat -an', host_tag: 'WEB01' }),
    );
  });

  it('should not submit empty commands', () => {
    fixture.detectChanges();
    component.newCommand = '  ';
    component.submitCommand();
    expect(mockApi.post).not.toHaveBeenCalled();
  });

  it('should clear command inputs after successful submit', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of({}));
    component.newCommand = 'whoami';
    component.commandHost = 'DC01';
    component.submitCommand();
    expect(component.newCommand).toBe('');
    expect(component.commandHost).toBe('');
  });

  // ── Stats ────────────────────────────────────────────────────────
  it('should populate stats signal from API', () => {
    fixture.detectChanges();
    expect(component.stats()?.active_analysts).toBe(4);
    expect(component.stats()?.objectives_completed).toBe(2);
    expect(component.stats()?.objectives_total).toBe(5);
  });

  // ── Instructor Inject ────────────────────────────────────────────
  it('should send an inject and show success', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of({}));

    component.injectType = 'dns_spike';
    component.injectDescription = 'DNS query storm';
    component.sendInject();

    expect(mockApi.post).toHaveBeenCalledWith(
      `/ops/exercises/${exerciseId}/inject`,
      jasmine.objectContaining({ inject_type: 'dns_spike', description: 'DNS query storm' }),
    );
    expect(mockNotify.success).toHaveBeenCalledWith('Inject sent');
  });

  it('should show error notification on inject failure', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(throwError(() => new Error('fail')));
    component.sendInject();
    expect(mockNotify.error).toHaveBeenCalledWith('Failed to send inject');
  });

  // ── Error handling ───────────────────────────────────────────────
  it('should handle API errors gracefully on load', () => {
    mockApi.get.and.returnValue(throwError(() => new Error('Network error')));
    expect(() => fixture.detectChanges()).not.toThrow();
    expect(component.annotations()).toEqual([]);
    expect(component.sharedCommands()).toEqual([]);
  });

  it('should notify on annotation submit failure', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(throwError(() => new Error('fail')));
    component.newAnnotation = 'Error test';
    component.submitAnnotation();
    expect(mockNotify.error).toHaveBeenCalledWith('Failed to create annotation');
  });

  // ── formatElapsed ────────────────────────────────────────────────
  it('formatElapsed should format seconds as H:MM:SS', () => {
    expect(component.formatElapsed(0)).toBe('0:00:00');
    expect(component.formatElapsed(61)).toBe('0:01:01');
    expect(component.formatElapsed(3661)).toBe('1:01:01');
  });

  // ── ngOnDestroy clears interval ──────────────────────────────────
  it('should clear refresh interval on destroy', () => {
    spyOn(window, 'clearInterval');
    fixture.detectChanges();
    expect((component as any).refreshInterval).not.toBeNull();
    component.ngOnDestroy();
    expect(window.clearInterval).toHaveBeenCalled();
  });

  // ── refreshAll triggers all loads ────────────────────────────────
  it('refreshAll should call all three load methods', () => {
    fixture.detectChanges();
    mockApi.get.calls.reset();
    component.refreshAll();
    const paths = mockApi.get.calls.allArgs().map(a => a[0]);
    expect(paths).toContain(`/ops/exercises/${exerciseId}/annotations`);
    expect(paths).toContain(`/ops/exercises/${exerciseId}/commands`);
    expect(paths).toContain(`/ops/exercises/${exerciseId}/stats`);
  });
});
