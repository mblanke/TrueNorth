import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  RangeLeaseStatus, RangeOperation, RangeRecoveryApiService,
} from '@core/services/range-recovery-api.service';
import { RangeCredentialsApiService } from '@core/services/range-credentials-api.service';
import { RangeDetailComponent, leftOnHypervisor } from './range-detail.component';
import { rangeCapabilities } from './range-capabilities';

const RANGE = {
  id: 'r1', name: 'blue-range', state: 'failed', template_id: 't1', created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z', error_message: 'provision abandoned by an operator', provisioner_backend: 'vsphere_api',
};
const OP_IN_FLIGHT: RangeOperation = {
  id: 'op2', range_id: 'r1', action: 'provision', generation: 2, status: 'dispatched', dispatch_attempts: 1,
  error: { code: 'no_outcome', message: 'No result from the worker' }, created_at: '2026-10-01T00:00:00Z',
};
const OP_DONE: RangeOperation = {
  id: 'op1', range_id: 'r1', action: 'provision', generation: 1, status: 'failed', dispatch_attempts: 1,
  error: { code: 'abandoned', message: 'Abandoned by admin' }, created_at: '2026-09-30T00:00:00Z',
};
const NO_LEASE: RangeLeaseStatus = { range_id: 'r1', kind: 'none', reason: 'No worker holds this range.' } as RangeLeaseStatus;
const QUARANTINED: RangeLeaseStatus = {
  range_id: 'r1', kind: 'tombstone', holder: 'abandoned:provision:abc', expires_at: '2026-10-12T10:00:00Z',
  blocking: true, force_release_allowed: false, reason: 'The abandoned worker is alive.',
  guidance: 'Quarantine the range instead: leave it blocked.',
} as RangeLeaseStatus;
const PROVEN: RangeLeaseStatus = {
  ...QUARANTINED, force_release_allowed: true, reason: 'The abandoned worker has not renewed the tombstone.', guidance: '',
} as RangeLeaseStatus;

describe('RangeDetailComponent', () => {
  let fixture: ComponentFixture<RangeDetailComponent>;
  let component: RangeDetailComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<RangeRecoveryApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let dialogOpen: jasmine.Spy;
  let dialogResult: { reason: string } | null;
  let credentialsApi: jasmine.SpyObj<RangeCredentialsApiService>;

  async function render(role: UserRole, opts: { ops?: RangeOperation[]; lease?: RangeLeaseStatus; fail?: boolean;
    platformAdmin?: boolean } = {}) {
    api = jasmine.createSpyObj('RangeRecoveryApiService', [
      'getRange', 'listOperations', 'getLease', 'listNetworkReservations', 'abandonOperation', 'forceReleaseLease',
      'abandonRange', 'listSnapshots', 'createSnapshot', 'deleteSnapshot', 'restoreSnapshot',
    ]);
    api.getRange.and.returnValue(of(RANGE as any));
    api.listOperations.and.returnValue(opts.fail
      ? throwError(() => new HttpErrorResponse({ status: 500, error: { detail: 'database unavailable' } }))
      : of(opts.ops ?? [OP_DONE]));
    api.getLease.and.returnValue(of(opts.lease ?? NO_LEASE));
    api.listNetworkReservations.and.returnValue(of([
      { kind: 'vlan', domain: 'dvs-1', value: '210', holder: 'r1' },
    ]));
    api.listSnapshots.and.returnValue(of([]));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    credentialsApi = jasmine.createSpyObj('RangeCredentialsApiService', ['getCredentials']);
    credentialsApi.getCredentials.and.returnValue(of({ range_id: 'r1', scope: 'all', accounts: [
      { machine: 'dc01', username: 'Administrator', password: 'Dc-Secret-1!', role: 'admin', student_visible: false,
        generated: true, applied: true, notes: '' }] }));
    dialogResult = { reason: 'vCenter checked: no tasks for this range' };
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of(dialogResult) }));

    await TestBed.configureTestingModule({
      imports: [RangeDetailComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: RangeRecoveryApiService, useValue: api },
        { provide: RangeCredentialsApiService, useValue: credentialsApi },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ id: 'u1', role }),
          isPlatformAdmin: signal(opts.platformAdmin ?? false) } },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeDetailComponent);
    fixture.componentRef.setInput('id', 'r1');
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const buttons = (text: string) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes(text)) as HTMLButtonElement[];

  it('loads the range, its operations, lease and reservations', async () => {
    await render('range_ops');
    expect(api.getRange).toHaveBeenCalledWith('r1');
    expect(el.textContent).toContain('blue-range');
    expect(el.textContent).toContain('provision abandoned by an operator');
    expect(el.textContent).toContain('Abandoned by admin');
    expect(el.textContent).toContain('No worker holds this range.');
    expect(el.textContent).toContain('dvs-1');
  });

  it('has a credentials panel that reads nothing until asked, then masks the passwords', async () => {
    await render('range_ops');
    expect(el.textContent).toContain('Credentials');
    expect(credentialsApi.getCredentials).not.toHaveBeenCalled();
    buttons('Show accounts')[0].click();
    fixture.detectChanges();
    expect(credentialsApi.getCredentials).toHaveBeenCalledOnceWith('r1');
    expect(el.textContent).toContain('Administrator');
    expect(el.textContent).not.toContain('Dc-Secret-1!');
  });

  it('shows the API error for a section instead of an empty list', async () => {
    await render('range_ops', { fail: true });
    expect(el.textContent).toContain('database unavailable');
    expect(el.textContent).not.toContain('No operations recorded');
  });

  it('abandons an in-flight operation only after stating the impact, then shows the outcome', async () => {
    await render('range_ops', { ops: [OP_IN_FLIGHT, OP_DONE] });
    api.abandonOperation.and.returnValue(of({ ...OP_IN_FLIGHT, status: 'failed',
      error: { code: 'abandoned', message: 'Abandoned by ops; the worker lease was fenced' } }));
    const [abandon] = buttons('Abandon');
    expect(abandon).toBeTruthy();
    abandon.click();
    const data = dialogOpen.calls.mostRecent().args[1].data;
    expect(data.impact.join(' ')).toContain('Check the hypervisor first');
    expect(api.abandonOperation).toHaveBeenCalledWith('r1', 'op2');
    fixture.detectChanges();
    expect(el.textContent).toContain('Operation 2 is now failed.');
    expect(el.textContent).toContain('worker lease was fenced');
    expect(api.listOperations).toHaveBeenCalledTimes(2); // re-read after the action
  });

  it('cancelling the impact dialog calls nothing', async () => {
    await render('range_ops', { ops: [OP_IN_FLIGHT] });
    dialogResult = null;
    buttons('Abandon')[0].click();
    expect(api.abandonOperation).not.toHaveBeenCalled();
  });

  it('an observer sees operations but no actions', async () => {
    await render('observer', { ops: [OP_IN_FLIGHT], lease: PROVEN });
    expect(el.textContent).toContain('provision');
    expect(buttons('Abandon').length).toBe(0);
    expect(buttons('Force-release').length).toBe(0);
    expect(el.querySelector('tn-range-snapshots form')).toBeNull();
  });

  it('does not offer force-release without the API\'s proof; shows the quarantine guidance', async () => {
    await render('admin', { lease: QUARANTINED });
    expect(buttons('Force-release').length).toBe(0);
    expect(el.textContent).toContain('Force-release is not offered');
    expect(el.textContent).toContain('Quarantine the range instead');
    expect(el.textContent).toContain('The abandoned worker is alive.');
  });

  it('offers force-release to an admin when proven, with a reason and the range name typed', async () => {
    await render('admin', { lease: PROVEN });
    api.forceReleaseLease.and.returnValue(of({
      range_id: 'r1', released_holder: 'abandoned:provision:abc', was_expired: false, warning: 'check vCenter',
    }));
    buttons('Force-release')[0].click();
    const data = dialogOpen.calls.mostRecent().args[1].data;
    expect(data.reasonMin).toBe(10);
    expect(data.typeToConfirm).toBe('blue-range');
    expect(api.forceReleaseLease).toHaveBeenCalledWith('r1', 'vCenter checked: no tasks for this range');
    fixture.detectChanges();
    expect(el.textContent).toContain('Released abandoned:provision:abc.');
    expect(el.textContent).toContain('check vCenter');
  });

  it('a range_ops user sees a proven lease but cannot force-release it', async () => {
    await render('range_ops', { lease: PROVEN });
    expect(buttons('Force-release').length).toBe(0);
    expect(el.textContent).toContain('An administrator can force-release this lease.');
  });

  it('offers Abandon range only to the platform administrator, not to every admin', async () => {
    await render('admin', { platformAdmin: false });
    expect(buttons('Abandon range').length).toBe(0);
    expect(el.textContent).not.toContain('Abandon the range');
    fixture.destroy();
    TestBed.resetTestingModule();
    await render('admin', { platformAdmin: true });
    expect(buttons('Abandon range').length).toBe(1);
  });

  it('abandons a failed range (platform admin) and lists what was left for manual cleanup', async () => {
    await render('admin', { platformAdmin: true });
    const abandoned = {
      ...RANGE, state: 'destroyed',
      provisioner_output: JSON.stringify({ abandoned: { vms: [{ name: 'r-dc01', vm_id: 'vm-1' }], networks: ['pg-210'] } }),
    };
    api.abandonRange.and.returnValue(of(abandoned as any));
    buttons('Abandon range')[0].click();
    const data = dialogOpen.calls.mostRecent().args[1].data;
    expect(data.impact.join(' ')).toContain('WITHOUT calling the hypervisor');
    expect(data.impact.join(' ')).toContain('removed by hand');
    expect(api.abandonRange).toHaveBeenCalledWith('r1', 'vCenter checked: no tasks for this range');
    fixture.detectChanges();
    expect(el.textContent).toContain('Range is now destroyed.');
    expect(el.textContent).toContain('r-dc01');
    expect(el.textContent).toContain('pg-210');
  });

  it('shows a refusal as a refusal, not a success', async () => {
    await render('admin', { platformAdmin: true });
    api.abandonRange.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 403, error: { detail: 'Only a platform administrator can change this' },
    })));
    buttons('Abandon range')[0].click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Refused by the API; nothing was changed.');
    expect(el.textContent).toContain('Only a platform administrator');
    expect(notify.error).toHaveBeenCalled();
  });

  it('re-reads on a timer only while something is in flight', async () => {
    await render('range_ops', { ops: [OP_DONE] });
    expect(component.polling()).toBeFalse();
    fixture.destroy();
    TestBed.resetTestingModule();
    await render('range_ops', { ops: [OP_IN_FLIGHT] });
    expect(component.polling()).toBeTrue();
    expect(el.textContent).toContain('re-reading every 10 s');
  });

  it('reads the permission table: the screen (PAGE_ACCESS.rangeRecovery) and each action', () => {
    expect(rangeCapabilities('student').view).toBeFalse();
    expect(rangeCapabilities('observer')).toEqual(
      { view: false, changeRange: false, deleteSnapshot: false, forceRelease: false, abandonRange: false });
    expect(rangeCapabilities('range_ops').view).toBeTrue();
    expect(rangeCapabilities('range_ops').deleteSnapshot).toBeTrue();
    expect(rangeCapabilities('instructor').view).toBeTrue();
    expect(rangeCapabilities('instructor').changeRange).toBeTrue();
    expect(rangeCapabilities('instructor').forceRelease).toBeFalse();
    expect(rangeCapabilities('admin').forceRelease).toBeTrue();
    expect(rangeCapabilities('admin').abandonRange).toBeFalse();
    expect(rangeCapabilities('admin', true).abandonRange).toBeTrue();
    expect(rangeCapabilities('instructor', true).abandonRange).toBeFalse();
  });

  it('reads nothing left behind from a range that was not abandoned', () => {
    expect(leftOnHypervisor({ ...RANGE, provisioner_output: '{}' } as any)).toEqual([]);
  });
});
