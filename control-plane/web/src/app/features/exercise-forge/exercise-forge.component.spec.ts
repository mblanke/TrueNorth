import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ExerciseForgeComponent } from './exercise-forge.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';

describe('ExerciseForgeComponent', () => {
  let component: ExerciseForgeComponent;
  let fixture: ComponentFixture<ExerciseForgeComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;

  const mockFeeds = [
    { id: 'f1', name: 'AlienVault OTX', source_type: 'otx', is_active: true },
    { id: 'f2', name: 'MISP Feed', source_type: 'misp', is_active: true },
  ];

  const mockPresets = [
    { name: 'Quick Detection Drill', difficulty: 'beginner', duration_minutes: 30, objective_count: 3, focus_areas: ['detection'], description: 'Short drill' },
    { name: 'Advanced Threat Hunt', difficulty: 'advanced', duration_minutes: 120, objective_count: 7, focus_areas: ['detection', 'analysis'], description: 'Deep-dive' },
  ];

  const mockPreview = {
    scenario_yaml: 'name: test\ntimeline:\n  - inject: phish',
    model_used: 'gpt-4o',
    indicators_used: 3,
    mitre_techniques: ['T1566.001', 'T1059.001'],
    estimated_duration_minutes: 60,
    objective_count: 4,
  };

  const mockResult = {
    exercise_id: 'e-123',
    scenario_id: 's-456',
    name: 'Forged: APT29 Campaign',
    scenario_yaml: 'name: APT29\ntimeline:\n  - inject: c2_beacon',
    model_used: 'gpt-4o',
    indicators_used: 5,
    mitre_techniques: ['T1566.001', 'T1059.001', 'T1071.001'],
  };

  const mockRanges = [
    { id: 'r-1', name: 'Alpha Range', state: 'ready' },
    { id: 'r-2', name: 'Bravo Range', state: 'stopped' },
  ];

  const mockHistory = {
    items: [
      {
        id: 'h-1',
        created_at: '2026-08-01T12:00:00Z',
        exercise_id: 'e-123',
        scenario_id: 's-456',
        exercise_name: 'Forged: APT29 Campaign',
        source: 'feed',
        difficulty: 'advanced',
        model_used: 'gpt-4o',
        mitre_techniques: ['T1566.001'],
      },
    ],
    total: 1,
  };

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService',
      ['get', 'post', 'listCurricula', 'listRanges', 'getForgeHistory']);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error']);

    mockApi.get.and.callFake((path: string): any => {
      if (path.includes('feeds')) return of(mockFeeds);
      if (path.includes('presets')) return of(mockPresets);
      return of([]);
    });
    mockApi.listCurricula.and.returnValue(of([]));
    mockApi.listRanges.and.returnValue(of(mockRanges as any));
    mockApi.getForgeHistory.and.returnValue(of(mockHistory));

    await TestBed.configureTestingModule({
      imports: [ExerciseForgeComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { queryParamMap: convertToParamMap({}) } },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ExerciseForgeComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should load feeds and presets on init', () => {
    fixture.detectChanges();
    expect(mockApi.get).toHaveBeenCalledWith('/threat-intel/feeds');
    expect(mockApi.get).toHaveBeenCalledWith('/exercise-forge/presets');
    expect(component.feeds().length).toBe(2);
    expect(component.presets().length).toBe(2);
  });

  it('should start on source step', () => {
    fixture.detectChanges();
    expect(component.currentStep()).toBe('source');
  });

  it('canProceedFromSource returns false when no feed selected', () => {
    component.sourceMode = 'feed';
    component.feedId = null;
    expect(component.canProceedFromSource()).toBeFalse();
  });

  it('canProceedFromSource returns true when feed is selected', () => {
    component.sourceMode = 'feed';
    component.feedId = 'f1';
    expect(component.canProceedFromSource()).toBeTrue();
  });

  it('canProceedFromSource returns true when manual indicators have values', () => {
    component.sourceMode = 'manual';
    component.manualIndicators = [
      { indicator_type: 'ipv4', value: '10.0.0.1', severity: 'high', mitre_attack_ids: [] },
    ];
    expect(component.canProceedFromSource()).toBeTrue();
  });

  it('applyPreset should fill config', () => {
    const preset = mockPresets[1];
    component.applyPreset(preset as any);
    expect(component.config.difficulty).toBe('advanced');
    expect(component.config.duration_minutes).toBe(120);
    expect(component.config.objective_count).toBe(7);
    expect(component.selectedPreset).toBe('Advanced Threat Hunt');
  });

  it('addIndicator should add an entry', () => {
    const before = component.manualIndicators.length;
    component.addIndicator();
    expect(component.manualIndicators.length).toBe(before + 1);
  });

  it('removeIndicator should remove an entry', () => {
    component.manualIndicators = [
      { indicator_type: 'ipv4', value: 'a', severity: 'low', mitre_attack_ids: [] },
      { indicator_type: 'domain', value: 'b', severity: 'high', mitre_attack_ids: [] },
    ];
    component.removeIndicator(0);
    expect(component.manualIndicators.length).toBe(1);
    expect(component.manualIndicators[0].value).toBe('b');
  });

  it('doPreview should call API and move to preview step', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of(mockPreview));
    component.sourceMode = 'feed';
    component.feedId = 'f1';

    component.doPreview();

    expect(mockApi.post).toHaveBeenCalledWith('/exercise-forge/preview', jasmine.objectContaining({
      feed_id: 'f1',
      difficulty: 'intermediate',
    }));
    expect(component.preview()).toEqual(mockPreview);
    expect(component.currentStep()).toBe('preview');
  });

  it('doPreview should show error on failure', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(throwError(() => ({ error: { detail: 'No indicators' } })));
    component.sourceMode = 'feed';
    component.feedId = 'f1';

    component.doPreview();

    expect(mockNotify.error).toHaveBeenCalledWith('No indicators');
    expect(component.previewing()).toBeFalse();
  });

  it('doGenerate should call API and move to result step', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of(mockResult));
    component.sourceMode = 'feed';
    component.feedId = 'f1';

    component.doGenerate();

    expect(mockApi.post).toHaveBeenCalledWith('/exercise-forge/generate', jasmine.objectContaining({
      feed_id: 'f1',
    }));
    expect(component.result()).toEqual(mockResult);
    expect(component.currentStep()).toBe('result');
    expect(mockNotify.success).toHaveBeenCalledWith('Exercise forged successfully!');
  });

  it('doGenerate should show error on failure', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(throwError(() => ({ error: { detail: 'Range not found' } })));
    component.sourceMode = 'feed';
    component.feedId = 'f1';

    component.doGenerate();

    expect(mockNotify.error).toHaveBeenCalledWith('Range not found');
    expect(component.generating()).toBeFalse();
  });

  it('should load ranges on init', () => {
    fixture.detectChanges();
    expect(mockApi.listRanges).toHaveBeenCalled();
    expect(component.ranges().length).toBe(2);
  });

  it('omits range_id from the payload when no range is picked', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of(mockResult));
    component.sourceMode = 'feed';
    component.feedId = 'f1';
    component.config.range_id = null;

    component.doGenerate();

    const body = mockApi.post.calls.mostRecent().args[1] as Record<string, unknown>;
    expect('range_id' in body).toBeFalse();
  });

  it('includes range_id in the payload when a range is picked', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of(mockResult));
    component.sourceMode = 'feed';
    component.feedId = 'f1';
    component.config.range_id = 'r-2';

    component.doGenerate();

    expect(mockApi.post).toHaveBeenCalledWith('/exercise-forge/generate', jasmine.objectContaining({
      range_id: 'r-2',
    }));
  });

  it('result panel links to the new exercise and its scenario', () => {
    fixture.detectChanges();
    mockApi.post.and.returnValue(of(mockResult));
    component.sourceMode = 'feed';
    component.feedId = 'f1';
    component.doGenerate();
    fixture.detectChanges();

    const hrefs = Array.from(
      fixture.nativeElement.querySelectorAll('[href]') as NodeListOf<HTMLAnchorElement>,
    ).map(a => a.getAttribute('href'));
    expect(hrefs).toContain('/exercises/e-123');
    expect(hrefs.some(h => h?.startsWith('/authoring/scenarios?scenario=s-456'))).toBeTrue();
  });

  it('loadHistory fetches once and populates rows', () => {
    fixture.detectChanges();
    component.loadHistory();
    component.loadHistory();

    expect(mockApi.getForgeHistory).toHaveBeenCalledTimes(1);
    expect(component.history().length).toBe(1);
    expect(component.historyTotal()).toBe(1);
    expect(component.historyLoading()).toBeFalse();
  });

  it('loadHistory surfaces an error and stays retryable', () => {
    fixture.detectChanges();
    mockApi.getForgeHistory.and.returnValue(throwError(() => new Error('boom')));

    component.loadHistory();

    expect(mockNotify.error).toHaveBeenCalledWith('Failed to load forge history');
    expect(component.historyLoading()).toBeFalse();

    mockApi.getForgeHistory.and.returnValue(of(mockHistory));
    component.loadHistory();
    expect(component.history().length).toBe(1);
  });

  it('reset should return to source step and clear state', () => {
    component.currentStep.set('result');
    component.result.set(mockResult);
    component.feedId = 'f1';

    component.reset();

    expect(component.currentStep()).toBe('source');
    expect(component.result()).toBeNull();
    expect(component.preview()).toBeNull();
    expect(component.feedId).toBeNull();
  });
});
