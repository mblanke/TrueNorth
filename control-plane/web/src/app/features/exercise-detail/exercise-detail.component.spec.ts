import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
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
    api = jasmine.createSpyObj('ApiService', ['get', 'listDetections', 'submitDetection', 'getRange', 'getRangeDiagram']);
    api.get.and.returnValue(of(detail(state)) as any);
    api.listDetections.and.returnValue(of(attempts));
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

  it('gives staff the full attempts table', async () => {
    const el = await render('instructor');
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Attempts (1)'))!.click();
    fixture.detectChanges();
    expect(el.querySelector('td.precision')?.textContent?.trim()).toBe('25%');
  });
});
