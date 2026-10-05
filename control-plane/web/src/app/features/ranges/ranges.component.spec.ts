import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { POLL_MS, RangesComponent } from './ranges.component';
import { ApiService, RangeOperation } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeEventsService, RangeStateEvent } from '@core/services/range-events.service';
import { Range, RangeSummary, Template } from '@core/models';

describe('RangesComponent', () => {
  let rangeEvents: Subject<RangeStateEvent>;
  let component: RangesComponent;
  let fixture: ComponentFixture<RangesComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;

  const mockRanges: Partial<Range>[] = [
    { id: 'r1', name: 'Range A', state: 'ready', created_at: '2026-01-15T10:00:00Z' },
    { id: 'r2', name: 'Range B', state: 'created', created_at: '2026-01-16T11:00:00Z' },
    { id: 'r3', name: 'Range C', state: 'provisioning', created_at: '2026-01-17T12:00:00Z' },
    { id: 'r4', name: 'Range D', state: 'destroyed', created_at: '2026-01-18T13:00:00Z' },
  ];

  const mockTemplates: Partial<Template>[] = [
    { id: 't1', name: 'Basic', version: '1.0' },
    { id: 't2', name: 'Advanced', version: '2.0' },
  ];

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService', [
      'listRanges',
      'listTemplates',
      'createRange',
      'provisionRange',
      'stopRange',
      'startRange',
      'destroyRange',
      'getRangeStats',
      'listRangeOperations',
      'abandonRangeOperation',
    ]);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);

    mockApi.listRanges.and.returnValue(of(mockRanges as Range[]));
    mockApi.listTemplates.and.returnValue(of(mockTemplates as Template[]));
    mockApi.createRange.and.returnValue(of({ id: 'r5', name: 'New', state: 'created' } as Range));
    mockApi.provisionRange.and.returnValue(of({ id: 'r2', state: 'provisioning' } as Range));
    mockApi.stopRange.and.returnValue(of({ id: 'r1', state: 'stopped' } as Range));
    mockApi.startRange.and.returnValue(of({ id: 'r5', state: 'running' } as Range));
    mockApi.destroyRange.and.returnValue(of({ id: 'r1', state: 'destroying' } as Range));
    mockApi.listRangeOperations.and.returnValue(of([]));
    mockApi.getRangeStats.and.returnValue(of({
      total_ranges: 4, by_state: { ready: 1, created: 1 }, total_vms: 12, active_exercises: 2,
    }));

    rangeEvents = new Subject<RangeStateEvent>();
    await TestBed.configureTestingModule({
      imports: [RangesComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
        { provide: RangeEventsService, useValue: { stream: () => rangeEvents.asObservable() } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RangesComponent);
    component = fixture.componentInstance;
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should create', () => {
    expect(component).toBeTruthy();
  });

  // ── Loads ranges on init ─────────────────────────────────────────
  it('should load ranges on init', () => {
    fixture.detectChanges();

    expect(mockApi.listRanges).toHaveBeenCalled();
    expect(component.ranges().length).toBe(4);
  });

  // ── Loads templates on init ──────────────────────────────────────
  it('should load templates on init', () => {
    fixture.detectChanges();

    expect(mockApi.listTemplates).toHaveBeenCalled();
    expect(component.templates().length).toBe(2);
  });

  // ── Provision button calls provisionRange ────────────────────────
  it('provision() should call provisionRange and show success', () => {
    fixture.detectChanges();

    component.provision('r2');

    expect(mockApi.provisionRange).toHaveBeenCalledWith('r2');
    expect(mockNotify.success).toHaveBeenCalledWith('Provisioning requested');
  });

  it('provision() should show error on failure', () => {
    mockApi.provisionRange.and.returnValue(throwError(() => new Error('fail')));
    fixture.detectChanges();

    component.provision('r2');

    expect(mockNotify.error).toHaveBeenCalledWith('Could not request provisioning');
  });

  // ── Destroy button calls destroyRange ────────────────────────────
  it('destroy() should call destroyRange and show notification', () => {
    fixture.detectChanges();

    component.destroy('r1');

    expect(mockApi.destroyRange).toHaveBeenCalledWith('r1');
    expect(mockNotify.success).toHaveBeenCalledWith('Destroy requested');
  });

  it('destroy() should show error on failure', () => {
    mockApi.destroyRange.and.returnValue(throwError(() => new Error('fail')));
    fixture.detectChanges();

    component.destroy('r1');

    expect(mockNotify.error).toHaveBeenCalledWith('Could not request destroy');
  });

  // ── Worker events ────────────────────────────────────────────────
  it('reloads the list when the worker reports a range state, once per burst', fakeAsync(() => {
    fixture.detectChanges();
    mockApi.listRanges.calls.reset();
    rangeEvents.next({ id: 'r1', state: 'ready', error: null });
    rangeEvents.next({ id: 'r2', state: 'ready', error: null });
    tick(300);
    expect(mockApi.listRanges).toHaveBeenCalledTimes(1);
    discardPeriodicTasks();
  }));

  // ── Stop / start: operations, like provision ─────────────────────
  it('stop() requests a stop operation and reloads; nothing claims the VMs are off yet', () => {
    fixture.detectChanges();

    component.stop('r1');

    expect(mockApi.stopRange).toHaveBeenCalledWith('r1');
    expect(mockNotify.success).toHaveBeenCalledWith('Stop requested');
    expect(mockApi.listRanges).toHaveBeenCalledTimes(2);
    expect(component.busy().has('r1')).toBeFalse();
  });

  it('start() requests a start operation and reloads', () => {
    fixture.detectChanges();

    component.start('r5');

    expect(mockApi.startRange).toHaveBeenCalledWith('r5');
    expect(mockNotify.success).toHaveBeenCalledWith('Start requested');
    expect(mockApi.listRanges).toHaveBeenCalledTimes(2);
  });

  it('start() shows the server detail on a 409', () => {
    mockApi.startRange.and.returnValue(throwError(() => ({
      status: 409, error: { detail: 'A stop of this range is still in progress' },
    })));
    fixture.detectChanges();

    component.start('r5');

    expect(mockNotify.error).toHaveBeenCalledWith('A stop of this range is still in progress');
    expect(mockNotify.success).not.toHaveBeenCalled();
    expect(component.busy().has('r5')).toBeFalse();
  });

  it('enables Start for stopped/failed and Stop for running/ready/failed; in progress disables both', () => {
    mockApi.listRanges.and.returnValue(of([
      { id: 'a', name: 'Stopped', state: 'stopped', created_at: '2026-01-15T10:00:00Z' },
      { id: 'b', name: 'Running', state: 'running', created_at: '2026-01-15T10:00:00Z' },
      { id: 'c', name: 'Ready', state: 'ready', created_at: '2026-01-15T10:00:00Z' },
      { id: 'd', name: 'Created', state: 'created', created_at: '2026-01-15T10:00:00Z' },
      { id: 'e', name: 'Stopping', state: 'stopping', created_at: '2026-01-15T10:00:00Z' },
      { id: 'f', name: 'Failed', state: 'failed', created_at: '2026-01-15T10:00:00Z' },
    ] as Range[]));
    mockApi.listRangeOperations.and.returnValue(of([]));
    fixture.detectChanges();
    const rows = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('tr.mat-mdc-row'));
    const btn = (row: Element, cls: string) => row.querySelector<HTMLButtonElement>('button.' + cls);
    const enabled = (row: Element) => [!btn(row, 'power-start')!.disabled, !btn(row, 'power-stop')!.disabled];

    expect(enabled(rows[0])).toEqual([true, false]);
    expect(enabled(rows[1])).toEqual([false, true]);
    expect(enabled(rows[2])).toEqual([false, true]);
    expect(btn(rows[3], 'power-start')).toBeNull();
    expect(btn(rows[3], 'power-stop')).toBeNull();
    expect(enabled(rows[4])).toEqual([false, false]);
    expect(enabled(rows[5])).toEqual([true, true]);
    expect(btn(rows[0], 'power-start')!.getAttribute('aria-label')).toBe('Start Stopped');

    btn(rows[0], 'power-start')!.click();
    expect(mockApi.startRange).toHaveBeenCalledWith('a');
  });

  it('describes an in-flight stop in words', () => {
    mockApi.listRanges.and.returnValue(of([
      { id: 'e', name: 'Stopping', state: 'stopping', created_at: '2026-01-15T10:00:00Z' },
    ] as Range[]));
    mockApi.listRangeOperations.and.returnValue(of([
      { id: 'op1', range_id: 'e', action: 'stop', status: 'dispatched', generation: 1, dispatch_attempts: 1 },
    ] as RangeOperation[]));
    fixture.detectChanges();

    expect(component.opText({ id: 'e', state: 'stopping' } as RangeSummary)).toBe('Stopping…');
  });

  // ── Create range ─────────────────────────────────────────────────
  it('createRange() should post and reload', () => {
    fixture.detectChanges();

    component.newName = 'New Range';
    component.selectedTemplateId = 't1';
    component.createRange();

    expect(mockApi.createRange).toHaveBeenCalledWith({
      name: 'New Range',
      template_id: 't1',
    });
    expect(mockNotify.success).toHaveBeenCalledWith('Range created');
    // listRanges called again to reload
    expect(mockApi.listRanges).toHaveBeenCalledTimes(2);
  });

  // ── Displays range list in table ─────────────────────────────────
  it('should render range rows in the table', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const rows = el.querySelectorAll('tr.mat-mdc-row');
    expect(rows.length).toBe(4);
  });

  // ── Table columns ────────────────────────────────────────────────
  it('should have the expected column set', () => {
    expect(component.displayedColumns).toEqual(['name', 'state', 'created', 'actions']);
  });

  // ── Filters by state (conceptual — component shows all) ─────────
  it('ranges signal should contain items of different states', () => {
    fixture.detectChanges();
    const states = component.ranges().map((r) => r.state);
    expect(states).toContain('ready');
    expect(states).toContain('created');
    expect(states).toContain('provisioning');
    expect(states).toContain('destroyed');
  });

  // ── Operations: the row says what the request is actually doing ──
  const op = (o: Partial<RangeOperation>): RangeOperation =>
    ({ id: 'op1', range_id: 'r3', action: 'provision', generation: 1, status: 'dispatched',
       dispatch_attempts: 1, error: null, ...o } as RangeOperation);

  function statusText(): string {
    const el = fixture.nativeElement.querySelector('[data-testid="op-status"]');
    return el ? el.textContent.trim() : '';
  }

  it('says a provision is waiting for the task queue instead of claiming progress', () => {
    mockApi.listRangeOperations.and.returnValue(of([op({ status: 'pending', error: { code: 'broker_unavailable' } })]));
    fixture.detectChanges();
    fixture.detectChanges();
    expect(mockApi.listRangeOperations).toHaveBeenCalledWith('r3');
    expect(statusText()).toBe('Queued: waiting for the task queue to come back');
  });

  it('offers to give up only when the worker never reported back', () => {
    mockApi.listRangeOperations.and.returnValue(of([op({ error: { code: 'no_outcome' } })]));
    mockApi.abandonRangeOperation.and.returnValue(of(op({ status: 'failed' })));
    fixture.detectChanges();
    fixture.detectChanges();
    expect(statusText()).toBe('No result from the worker: check the hypervisor');
    const btn: HTMLButtonElement = fixture.nativeElement.querySelector('[data-testid="abandon"]');
    expect(btn).toBeTruthy();
    btn.click();
    expect(mockApi.abandonRangeOperation).toHaveBeenCalledWith('r3', 'op1');
  });

  it('shows no abandon button for an operation that is simply running', () => {
    mockApi.listRangeOperations.and.returnValue(of([op({})]));
    fixture.detectChanges();
    fixture.detectChanges();
    expect(statusText()).toBe('Provisioning…');
    expect(fixture.nativeElement.querySelector('[data-testid="abandon"]')).toBeNull();
  });

  it('shows the server\'s reason when a request is refused', () => {
    mockApi.provisionRange.and.returnValue(throwError(() => ({ status: 409, error: { detail: 'A destroy of this range is still in progress' } })));
    fixture.detectChanges();
    component.provision('r2');
    expect(mockNotify.error).toHaveBeenCalledWith('A destroy of this range is still in progress');
  });

  it('refreshes while a range is in progress, and not otherwise', fakeAsync(() => {
    fixture.detectChanges();
    const calls = mockApi.listRanges.calls.count();
    const statCalls = mockApi.getRangeStats.calls.count();
    tick(POLL_MS);
    expect(mockApi.listRanges.calls.count()).toBe(calls + 1);
    expect(mockApi.getRangeStats.calls.count()).toBe(statCalls + 1, 'the counts strip refreshes with the list');
    mockApi.listRanges.and.returnValue(of([{ id: 'r1', name: 'A', state: 'ready' } as Range]));
    tick(POLL_MS);
    const settled = mockApi.listRanges.calls.count();
    tick(POLL_MS * 3);
    expect(mockApi.listRanges.calls.count()).toBe(settled);
    discardPeriodicTasks();
  }));
});
