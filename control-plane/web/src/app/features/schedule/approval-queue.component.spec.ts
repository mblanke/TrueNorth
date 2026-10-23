import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ApprovalItem, SchedulerApiService } from '@core/services/scheduler-api.service';
import { ClassesApiService, PendingAddition } from '@core/services/classes-api.service';
import { ApprovalQueueComponent, approvalError } from './approval-queue.component';

const START = '2030-03-04T09:00:00Z';
const END = '2030-03-04T12:00:00Z';

function item(p: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    event: {
      id: 'e1', name: 'Lab 1', state: 'pending_approval', tenant_id: 't', range_id: null, template_id: null,
      description: null, start_time: START, end_time: END, vm_count: 4, vcpu_total: 40, ram_mb_total: 8192,
      disk_gb_total: 100, created_at: '', updated_at: '', class_id: 'c1', approval: {},
    },
    class_name: 'C201-26-03',
    fit: { fits: true, reasons: [], next_free: { start_time: START, end_time: END }, searched_until: START },
    ...p,
  } as ApprovalItem;
}

describe('ApprovalQueueComponent', () => {
  let fixture: ComponentFixture<ApprovalQueueComponent>;
  let component: ApprovalQueueComponent;
  let scheduler: jasmine.SpyObj<SchedulerApiService>;
  let classes: jasmine.SpyObj<ClassesApiService>;
  const text = () => fixture.nativeElement.textContent as string;
  const button = (label: string) =>
    Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>).find(b => b.textContent!.trim() === label);

  function render(items: ApprovalItem[], additions: PendingAddition[] = []) {
    scheduler.approvals.and.returnValue(of({ items }));
    classes.pendingAdditions.and.returnValue(of(additions));
    fixture = TestBed.createComponent(ApprovalQueueComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  beforeEach(async () => {
    scheduler = jasmine.createSpyObj('SchedulerApiService', ['approvals', 'approve', 'reject', 'propose']);
    scheduler.approve.and.returnValue(of({} as any));
    scheduler.reject.and.returnValue(of({} as any));
    scheduler.propose.and.returnValue(of({} as any));
    classes = jasmine.createSpyObj('ClassesApiService', ['pendingAdditions', 'approveAddition', 'rejectAddition']);
    classes.approveAddition.and.returnValue(of({} as any));
    classes.rejectAddition.and.returnValue(of({} as any));
    await TestBed.configureTestingModule({
      imports: [ApprovalQueueComponent],
      providers: [
        { provide: SchedulerApiService, useValue: scheduler },
        { provide: ClassesApiService, useValue: classes },
      ],
    }).compileComponents();
  });

  it('lists sessions awaiting approval and approves one', () => {
    render([item()]);
    expect(text()).toContain('Lab 1');
    expect(text()).toContain('C201-26-03');
    expect(text()).toContain('Fits');
    let changed = 0;
    component.changed.subscribe(() => changed++);
    button('Approve')!.click();
    expect(scheduler.approve).toHaveBeenCalledWith('e1');
    expect(changed).toBe(1);
  });

  it('shows why a session does not fit and its next free slot', () => {
    render([item({ fit: { fits: false, reasons: ['vCPU: need 40, 20 free'], next_free: { start_time: '2030-03-04T13:00:00Z', end_time: '2030-03-04T16:00:00Z' }, searched_until: START } })]);
    expect(text()).toContain('Doesn’t fit');
    expect(text()).toContain('vCPU: need 40, 20 free');
    expect(text()).toContain('Next free:');
  });

  it('needs a reason to reject', () => {
    render([item()]);
    button('Reject')!.click();
    fixture.detectChanges();
    const reject = button('Reject')!;
    expect(reject.disabled).toBeTrue();
    component.patch({ reason: 'Exercise week' });
    fixture.detectChanges();
    button('Reject')!.click();
    expect(scheduler.reject).toHaveBeenCalledWith('e1', 'Exercise week');
  });

  it('proposes the next free slot by default', () => {
    const next = { start_time: '2030-03-05T09:00:00Z', end_time: '2030-03-05T12:00:00Z' };
    render([item({ fit: { fits: false, reasons: ['x'], next_free: next, searched_until: START } })]);
    button('Propose time')!.click();
    fixture.detectChanges();
    button('Send proposal')!.click();
    const [id, start, end] = scheduler.propose.calls.mostRecent().args;
    expect(id).toBe('e1');
    expect(start.getTime()).toBe(Date.parse(next.start_time));
    expect(end.getTime()).toBe(Date.parse(next.end_time));
  });

  it('shows the server\'s refusal with its next free slot', () => {
    scheduler.approve.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: { message: 'Cannot approve: it does not fit the cluster', reasons: ['vCPU: need 40, 20 free'], next_free: { start_time: START, end_time: END } } },
    })));
    render([item()]);
    button('Approve')!.click();
    fixture.detectChanges();
    expect(text()).toContain('Cannot approve: it does not fit the cluster');
    expect(text()).toContain('Next free:');
  });

  it('decides roll-call additions that did not fit', () => {
    render([], [{ entry_id: 'r1', class_id: 'c1', class_name: 'C201-26-03', email: 'late@corp.test', reason: 'no room', added_by: null, created_at: null }]);
    expect(text()).toContain('Nothing awaiting approval');
    expect(text()).toContain('late@corp.test');
    button('Approve')!.click();
    expect(classes.approveAddition).toHaveBeenCalledWith('c1', 'r1');
  });

  it('reads plain and structured errors', () => {
    expect(approvalError(new HttpErrorResponse({ status: 403, error: { detail: 'Missing permissions' } }))).toBe('Missing permissions');
    expect(approvalError(new HttpErrorResponse({ status: 0 }))).toContain('could not be reached');
  });
});
