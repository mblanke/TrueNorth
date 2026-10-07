import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { DetectionPanelComponent, canonicalValidator, isDetectionObjective } from './detection-panel.component';

function attempt(over: Partial<Detection> = {}): Detection {
  return {
    id: 'd1', objective_ref: 'OBJ-1', user_id: '11111111-2222-3333-4444-555555555555',
    query: 'process.name:powershell.exe', submitted_at: '2026-10-06T10:00:00Z',
    verdict: 'missed', events_matched: 12, on_target: null, precision: null, attempts_left: null, reason: null,
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

  it('reports an achieved detection and asks the parent to refresh', () => {
    api.submitDetection.and.returnValue(of(attempt({ verdict: 'achieved', attempts_left: 4 })));
    setup();
    submit();
    expect(api.submitDetection).toHaveBeenCalledWith('ex1', 'OBJ-1', 'process.name:powershell.exe');
    expect(msg()).toContain('found the attack');
    expect(changed).toBe(1);
  });

  it('reports a missed detection with events matched and attempts left', () => {
    api.submitDetection.and.returnValue(of(attempt({ verdict: 'missed', events_matched: 12, attempts_left: 3 })));
    setup();
    submit();
    expect(msg()).toBe('Your query matched 12 events but did not find enough of the attack. 3 attempts left.');
    expect(changed).toBe(1);
  });

  it('says nothing was credited when the exercise closed while judging', () => {
    api.submitDetection.and.returnValue(of(attempt({ verdict: 'closed', attempts_left: 4 })));
    setup();
    submit();
    expect(msg()).toBe('The exercise closed before your detection was judged; nothing was credited.');
  });

  it('shows why a query did not parse (422) and that no attempt was used', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: 'Your query could not be parsed (no attempt used): Failed to parse query' },
    })));
    setup();
    submit();
    expect(msg()).toContain('no attempt used');
  });

  it('disables submitting once no attempts are left (429)', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 429, error: { detail: 'No attempts left for this objective (5 used)' },
    })));
    setup();
    submit();
    expect(msg()).toContain('No attempts left');
    expect(component.attemptsLeft()).toBe(0);
    expect(submitToggle()!.disabled).toBeTrue();
  });

  it('disables submitting when the last attempt was used', () => {
    api.submitDetection.and.returnValue(of(attempt({ verdict: 'missed', attempts_left: 0 })));
    setup();
    submit();
    expect(msg()).toContain('0 attempts left');
    expect(submitToggle()!.disabled).toBeTrue();
  });

  it('says try again when the event store is down (503) and does not refresh', () => {
    api.submitDetection.and.returnValue(throwError(() => new HttpErrorResponse({ status: 503, error: { detail: 'x' } })));
    setup();
    submit();
    expect(msg()).toContain('Try again');
    expect(msg()).toContain('no attempt was used');
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

  it('lists a Student their own attempts without precision', () => {
    setup({ attempts: [attempt()] });
    buttons().find(b => b.textContent?.includes('Attempts (1)'))!.click();
    fixture.detectChanges();
    const headers = Array.from(el().querySelectorAll('th')).map(h => h.textContent?.trim());
    expect(headers).toEqual(['Time', 'Query', 'Verdict', 'Events']);
    expect(el().querySelector('td.q')?.textContent).toContain('process.name:powershell.exe');
  });

  it('shows staff the Student, on target and precision', () => {
    setup({ staff: true, canSubmit: true, attempts: [attempt({ on_target: 9, precision: 0.75 })] });
    buttons().find(b => b.textContent?.includes('Attempts (1)'))!.click();
    fixture.detectChanges();
    const headers = Array.from(el().querySelectorAll('th')).map(h => h.textContent?.trim());
    expect(headers).toEqual(['Time', 'Student', 'Query', 'Verdict', 'Events', 'On target', 'Precision']);
    expect(el().querySelector('td.precision')?.textContent?.trim()).toBe('75%');
    expect(el().textContent).toContain('11111111');
  });
});
