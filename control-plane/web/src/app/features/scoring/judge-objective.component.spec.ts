import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { ApiService, Detection } from '@core/services/api.service';
import { Objective } from '@core/models';
import { JudgeObjectiveComponent, JudgeableObjective } from './judge-objective.component';

function sub(over: Partial<Detection> = {}): Detection {
  return {
    id: 'd1', objective_ref: 'detect_c2', user_id: '11111111-2222-3333-4444-555555555555',
    query: 'process.name:beacon.exe', submitted_at: '2026-11-03T10:00:00Z', verdict: 'recorded',
    events_matched: 4, submissions_left: null, ...over,
  };
}

const detection: JudgeableObjective = { ref_id: 'detect_c2', description: 'Detect C2', validator: 'opensearch_query', achieved: false };
const manual: JudgeableObjective = { ref_id: 'write_report', description: 'Write report', validator: 'manual_ack', achieved: false };

describe('JudgeObjectiveComponent (ADR 0020)', () => {
  let fixture: ComponentFixture<JudgeObjectiveComponent>;
  let component: JudgeObjectiveComponent;
  let api: jasmine.SpyObj<ApiService>;
  let dialog: jasmine.SpyObj<MatDialog>;
  let confirmed: boolean;
  let changed: number;

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const button = (text: string) => Array.from(el().querySelectorAll('button')).find(b => b.textContent?.includes(text));

  function setup(objective: JudgeableObjective, inputs: Record<string, unknown> = {}): void {
    fixture.componentRef.setInput('exerciseId', 'ex1');
    fixture.componentRef.setInput('objective', objective);
    for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
    fixture.detectChanges();
  }

  function openForm(): void {
    button('Acknowledge')!.click();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['ackObjective', 'withdrawAck']);
    confirmed = true;
    dialog = jasmine.createSpyObj('MatDialog', ['open']);
    dialog.open.and.callFake((() => ({ afterClosed: () => of(confirmed) })) as never);
    await TestBed.configureTestingModule({
      imports: [JudgeObjectiveComponent, NoopAnimationsModule],
      providers: [{ provide: ApiService, useValue: api }, { provide: MatDialog, useValue: dialog }],
    }).compileComponents();
    fixture = TestBed.createComponent(JudgeObjectiveComponent);
    component = fixture.componentInstance;
    changed = 0;
    component.changed.subscribe(() => changed++);
  });

  it('opens a note and the recorded submissions to cite on a detection objective', () => {
    setup(detection, { submissions: [sub(), sub({ id: 'd2', verdict: 'cited' }), sub({ id: 'd3', verdict: 'missed' })] });
    openForm();
    expect(el().querySelector('textarea[name="note"]')).toBeTruthy();
    expect(el().querySelector('legend')?.textContent).toContain('Detections to cite');
    // Recorded and cited rows are citable; an old ADR 0005 "missed" row is history only.
    expect(el().querySelectorAll('mat-checkbox').length).toBe(2);
    expect(el().textContent).toContain('process.name:beacon.exe');
    expect(el().textContent).toContain('11111111');
  });

  it('sends the note and the cited submissions, then asks the parent to reload', () => {
    api.ackObjective.and.returnValue(of({} as Objective));
    setup(detection, { submissions: [sub(), sub({ id: 'd2' })] });
    openForm();
    component.note = '  Team blocked the C2 domain  ';
    component.toggle('d2');
    component.acknowledge();
    fixture.detectChanges();
    expect(api.ackObjective).toHaveBeenCalledWith('ex1', 'detect_c2', { evidence: 'Team blocked the C2 domain', cites: ['d2'] });
    expect(changed).toBe(1);
    expect(el().querySelector('form')).toBeNull();
  });

  it('judges a non-detection objective with a note only and no cite list', () => {
    api.ackObjective.and.returnValue(of({} as Objective));
    setup(manual);
    openForm();
    expect(el().querySelector('fieldset.cites')).toBeNull();
    component.acknowledge();
    expect(api.ackObjective).toHaveBeenCalledWith('ex1', 'write_report', {});
  });

  it('says when there is nothing recorded, and when the submissions failed to load', () => {
    setup(detection);
    openForm();
    expect(el().textContent).toContain('No detections recorded on this objective in this run.');
    fixture.componentRef.setInput('submissionsError', true);
    fixture.detectChanges();
    expect(el().textContent).toContain('could not be loaded');
    expect(el().textContent).not.toContain('No detections recorded');
  });

  it('caps cites at 20', () => {
    const many = Array.from({ length: 21 }, (_, i) => sub({ id: `d${i}` }));
    setup(detection, { submissions: many });
    for (const d of many) component.toggle(d.id);
    expect(component.cited().size).toBe(20);
  });

  it('shows the server reason for a refused cite (422) and keeps the form open', () => {
    api.ackObjective.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: 'Cited submission d9 is not a recorded detection on this objective in this run' },
    })));
    setup(detection, { submissions: [sub()] });
    openForm();
    component.acknowledge();
    fixture.detectChanges();
    expect(el().querySelector('[role="alert"]')?.textContent).toContain('not a recorded detection');
    expect(el().querySelector('form')).toBeTruthy();
    expect(changed).toBe(0);
  });

  it('refreshes the page on 409 (exercise closed or already judged)', () => {
    api.ackObjective.and.returnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Exercise is completed' } })));
    setup(manual);
    openForm();
    component.acknowledge();
    expect(changed).toBe(1);
  });

  it('withdraws an achieved objective only after confirming', () => {
    api.withdrawAck.and.returnValue(of({} as Objective));
    setup({ ...detection, achieved: true });
    expect(button('Acknowledge')).toBeUndefined();
    confirmed = false;
    button('Withdraw')!.click();
    expect(dialog.open).toHaveBeenCalled();
    expect(api.withdrawAck).not.toHaveBeenCalled();
    confirmed = true;
    button('Withdraw')!.click();
    expect(api.withdrawAck).toHaveBeenCalledWith('ex1', 'detect_c2');
    expect(changed).toBe(1);
  });

  it('says a withdrawal failed rather than pretending it worked', () => {
    api.withdrawAck.and.returnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    setup({ ...detection, achieved: true });
    button('Withdraw')!.click();
    fixture.detectChanges();
    expect(el().querySelector('[role="alert"]')?.textContent).toContain('The judgement was not withdrawn.');
    expect(changed).toBe(0);
  });
});
