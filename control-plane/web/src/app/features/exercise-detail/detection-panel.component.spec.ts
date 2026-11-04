import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { DetectionPanelComponent, canonicalValidator, isDetectionObjective, verdictWord } from './detection-panel.component';

function attempt(over: Partial<Detection> = {}): Detection {
  return {
    id: 'd1', objective_ref: 'OBJ-1', user_id: '11111111-2222-3333-4444-555555555555',
    query: 'process.name:powershell.exe', submitted_at: '2026-10-06T10:00:00Z',
    verdict: 'recorded', events_matched: 12, submissions_left: null,
    ...over,
  };
}

describe('validator names', () => {
  it('normalises legacy spellings to opensearch_query', () => {
    expect(canonicalValidator('validate.opensearch.query')).toBe('opensearch_query');
    expect(canonicalValidator('validate_opensearch_query')).toBe('opensearch_query');
    expect(isDetectionObjective('validate.opensearch_query')).toBeTrue();
    expect(isDetectionObjective('opensearch_query')).toBeTrue();
    expect(isDetectionObjective('manual_ack')).toBeFalse();
    expect(isDetectionObjective(null)).toBeFalse();
  });
});

describe('verdictWord', () => {
  it('reads recorded and cited, and keeps older verdicts as history', () => {
    expect(verdictWord('recorded', false)).toBe('Recorded');
    expect(verdictWord('cited', false)).toBe('Cited by your instructor');
    expect(verdictWord('cited', true)).toBe('Cited');
    expect(verdictWord('missed', false)).toBe('Missed');
  });
});

describe('DetectionPanelComponent', () => {
  let fixture: ComponentFixture<DetectionPanelComponent>;
  let component: DetectionPanelComponent;
  let api: jasmine.SpyObj<ApiService>;
  let changed: number;

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const buttons = (): HTMLButtonElement[] => Array.from(el().querySelectorAll('button'));
  const submitToggle = () => buttons().find(b => b.textContent?.includes('Submit detection'));
  const msg = () => el().querySelector('.det-msg')?.textContent?.trim() ?? '';

  function setup(inputs: Record<string, unknown> = {}): void {
    fixture.componentRef.setInput('exerciseId', 'ex1');
    fixture.componentRef.setInput('refId', 'OBJ-1');
    fixture.componentRef.setInput('running', true);
    fixture.componentRef.setInput('canSubmit', true);
    for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
    fixture.detectChanges();
  }

  function submit(query = 'process.name:powershell.exe'): void {
    submitToggle()!.click();
    fixture.detectChanges();
    component.query = query;
    component.submit();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['submitDetection']);
    await TestBed.configureTestingModule({
      imports: [DetectionPanelComponent, NoopAnimationsModule],
      providers: [{ provide: ApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(DetectionPanelComponent);
    component = fixture.componentInstance;
    changed = 0;
    component.changed.subscribe(() => changed++);
  });

  it('opens a monospace query form', () => {
    setup();
    submitToggle()!.click();
    fixture.detectChanges();
    expect(el().querySelector('textarea[name="query"]')).toBeTruthy();
  });

  it('says the detection was recorded and the instructor judges, and asks the parent to refresh', () => {
    api.submitDetection.and.returnValue(of(attempt({ events_matched: 12, submissions_left: 18 })));
    setup();
    submit();
    expect(api.submitDetection).toHaveBeenCalledWith('ex1', 'OBJ-1', 'process.name:powershell.exe');
    expect(msg()).toBe('Recorded: your query matched 12 events. Your instructor judges the objective.');
    expect(msg()).not.toMatch(/achieved|missed|attempt/i);
    expect(changed).toBe(1);
  });

  it('says how many submissions are left once few remain', () => {
    api.submitDetection.and.returnValue(of(attempt({ events_matched: 1, submissions_left: 2 })));
    setup();
    submit();
    expect(msg()).toBe('Recorded: your query matched 1 event. Your instructor judges the objective. 2 submissions left.');
  });

  it('shows the server reason for a query outside the language (422) and that nothing was recorded', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: 'Your query could not be parsed (nothing recorded): Failed to parse query' },
    })));
    setup();
    submit();
    expect(msg()).toBe('Your query could not be parsed (nothing recorded): Failed to parse query');
    expect(changed).toBe(0);
  });

  it('adds "Nothing was recorded" when a 422 detail does not say so', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: 'Field event.label is not searchable' },
    })));
    setup();
    submit();
    expect(msg()).toBe('Field event.label is not searchable Nothing was recorded.');
  });

  it('disables submitting once the submission bound is reached (429)', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 429, error: { detail: 'You have submitted 20 detections for this objective in this run' },
    })));
    setup();
    submit();
    expect(msg()).toContain('20 detections');
    expect(component.submissionsLeft()).toBe(0);
    expect(submitToggle()!.disabled).toBeTrue();
  });

  it('disables submitting when the last submission was used', () => {
    api.submitDetection.and.returnValue(of(attempt({ submissions_left: 0 })));
    setup();
    submit();
    expect(msg()).toContain('0 submissions left');
    expect(submitToggle()!.disabled).toBeTrue();
  });

  it('says try again when the event store is down (503) and does not refresh', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({ status: 503, error: { detail: 'x' } })));
    setup();
    submit();
    expect(msg()).toContain('Try again');
    expect(msg()).toContain('nothing was recorded');
    expect(changed).toBe(0);
  });

  it('shows the 409 reason and refreshes the stale page', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: 'Objective already achieved' },
    })));
    setup();
    submit();
    expect(msg()).toBe('Objective already achieved');
    expect(changed).toBe(1);
  });

  it('hides the submit action when the exercise is not running', () => {
    setup({ running: false });
    expect(submitToggle()).toBeUndefined();
  });

  it('hides the submit action for a role without detection:submit', () => {
    setup({ canSubmit: false });
    expect(submitToggle()).toBeUndefined();
  });

  it('hides the submit action once the objective is achieved', () => {
    setup({ achieved: true });
    expect(submitToggle()).toBeUndefined();
  });

  it('lists a Student their own submissions, and one an instructor cited', () => {
    setup({ attempts: [attempt(), attempt({ id: 'd2', verdict: 'cited' })] });
    buttons().find(b => b.textContent?.includes('Submissions (2)'))!.click();
    fixture.detectChanges();
    const headers = Array.from(el().querySelectorAll('th')).map(h => h.textContent?.trim());
    expect(headers).toEqual(['Time', 'Query', 'Status', 'Events']);
    expect(el().querySelector('td.q')?.textContent).toContain('process.name:powershell.exe');
    const status = Array.from(el().querySelectorAll('td.v')).map(t => t.textContent?.trim());
    expect(status).toEqual(['Recorded', 'Cited by your instructor']);
  });

  it('shows staff which Student submitted', () => {
    setup({ staff: true, canSubmit: true, attempts: [attempt()] });
    buttons().find(b => b.textContent?.includes('Submissions (1)'))!.click();
    fixture.detectChanges();
    const headers = Array.from(el().querySelectorAll('th')).map(h => h.textContent?.trim());
    expect(headers).toEqual(['Time', 'Student', 'Query', 'Status', 'Events']);
    expect(el().textContent).toContain('11111111');
  });
});
