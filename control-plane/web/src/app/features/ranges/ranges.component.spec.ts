import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { POLL_MS, RangesComponent } from './ranges.component';
import { ApiService, RangeOperation } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Range, Template } from '@core/models';

describe('RangesComponent', () => {
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
    mockApi.destroyRange.and.returnValue(of({ id: 'r1', state: 'destroying' } as Range));
    mockApi.listRangeOperations.and.returnValue(of([]));
    mockApi.getRangeStats.and.returnValue(of({
      total_ranges: 4, by_state: { ready: 1, created: 1 }, total_vms: 12, active_exercises: 2,
    }));

    await TestBed.configureTestingModule({
      imports: [RangesComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
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

  // ── Stop ─────────────────────────────────────────────────────────
  it('stop() should call stopRange and show notification', () => {
    fixture.detectChanges();

    component.stop('r1');

    expect(mockApi.stopRange).toHaveBeenCalledWith('r1');
    expect(mockNotify.success).toHaveBeenCalledWith('Range marked stopped. Its VMs were not powered off.');
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
    tick(POLL_MS);
    expect(mockApi.listRanges.calls.count()).toBe(calls + 1);
    mockApi.listRanges.and.returnValue(of([{ id: 'r1', name: 'A', state: 'ready' } as Range]));
    tick(POLL_MS);
    const settled = mockApi.listRanges.calls.count();
    tick(POLL_MS * 3);
    expect(mockApi.listRanges.calls.count()).toBe(settled);
    discardPeriodicTasks();
  }));
});
