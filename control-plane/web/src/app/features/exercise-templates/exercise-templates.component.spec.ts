import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { ApiService } from '@core/services/api.service';
import { ContentPortApiService } from '@core/services/content-port-api.service';
import { ExerciseTemplatesApiService, ExerciseTemplateRow } from '@core/services/exercise-templates-api.service';
import { NotificationService } from '@core/services/notification.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { ExerciseTemplatesComponent } from './exercise-templates.component';

const ROWS = [
  { id: 't1', name: 'IR drill', description: '', revision: 2, is_public: false, shared: false, scenario_id: null,
    range_template_id: null, team_names: ['Blue', 'White cell'], objectives: 2, mesl_events: 3, tags: [],
    updated_at: '2026-10-28T10:00:00Z' },
] as unknown as ExerciseTemplateRow[];

describe('ExerciseTemplatesComponent', () => {
  let api: jasmine.SpyObj<ExerciseTemplatesApiService>;

  async function setup() {
    api = jasmine.createSpyObj('ExerciseTemplatesApiService', ['list', 'createExercise']);
    api.list.and.returnValue(of(ROWS));
    const port = jasmine.createSpyObj('ContentPortApiService', ['proven']);
    port.proven.and.returnValue(of({ proven: true, label: 'Proven', conditions: [], dependencies: [], golden_builds: [] }));
    const apiService = jasmine.createSpyObj('ApiService', ['listRanges']);
    apiService.listRanges.and.returnValue(of([{ id: 'r1', name: 'Range A', state: 'ready' }]));
    await TestBed.configureTestingModule({
      imports: [ExerciseTemplatesComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ExerciseTemplatesApiService, useValue: api },
        { provide: ContentPortApiService, useValue: port },
        { provide: ApiService, useValue: apiService },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: fakeAuth('instructor') },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(ExerciseTemplatesComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('lists templates with their counts, teams and Proven chip', async () => {
    const f = await setup();
    const text = (f.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('IR drill');
    expect(text).toContain('2 objective(s)');
    expect(text).toContain('3 serial(s)');
    expect(text).toContain('Blue, White cell');
    expect(f.nativeElement.querySelector('tn-proven-chip')).not.toBeNull();
  });

  it('offers Create exercise to staff and opens the create form', async () => {
    const f = await setup();
    const btn = Array.from((f.nativeElement as HTMLElement).querySelectorAll('button'))
      .find(b => b.textContent?.includes('Create exercise')) as HTMLButtonElement;
    btn.click();
    f.detectChanges();
    expect((f.nativeElement as HTMLElement).querySelector('tn-create-from-template')).not.toBeNull();
  });

  it('shows a retry when the list cannot be loaded', async () => {
    api = jasmine.createSpyObj('ExerciseTemplatesApiService', ['list']);
    api.list.and.returnValue(throwError(() => new Error('x')));
    await TestBed.configureTestingModule({
      imports: [ExerciseTemplatesComponent, NoopAnimationsModule],
      providers: [provideRouter([]), { provide: ExerciseTemplatesApiService, useValue: api },
        { provide: AuthService, useValue: fakeAuth('instructor') }],
    }).compileComponents();
    const f = TestBed.createComponent(ExerciseTemplatesComponent);
    f.detectChanges();
    expect((f.nativeElement as HTMLElement).textContent).toContain('Templates could not be loaded');
  });
});
