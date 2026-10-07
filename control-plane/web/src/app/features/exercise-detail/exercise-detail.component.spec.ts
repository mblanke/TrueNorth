import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { ExerciseDetailComponent } from './exercise-detail.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { InjectRecord } from '@core/models';

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
    mockApi = jasmine.createSpyObj('ApiService', ['get', 'listInjects', 'getRange', 'getRangeDiagram']);
    mockApi.get.and.returnValue(of(detail as any));
    mockApi.listInjects.and.returnValue(listInjects);
    await TestBed.configureTestingModule({
      imports: [ExerciseDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
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
