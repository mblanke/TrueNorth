import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeInventoryApiService, RangeInventoryChecks } from '@core/services/range-inventory-api.service';
import { RangeInventoryComponent } from './range-inventory.component';

const AT = '2026-10-22T12:00:00Z';

function checks(canConfirm: boolean, blocked: string | null = null): RangeInventoryChecks {
  return {
    range_id: 'r1',
    can_confirm: canConfirm,
    confirm_blocked: blocked,
    drift: {
      status: 'incomplete', summary: '1 change(s) from the record: memory_changed', checked_at: AT, changed_at: AT,
      findings: [{ kind: 'memory_changed', object: 'r-web', expected: 4096, actual: 8192 }],
      checked: { vms: 2, portgroups: 2 }, gaps: [],
    },
    teardown: {
      status: 'incomplete', summary: 'zero recorded ids remain; 2 unidentified item(s)', checked_at: AT,
      changed_at: AT, legacy: false, deleted: [{ kind: 'vm', id: 'vm-1', name: 'r-web', marks: [] }],
      remaining: [], not_checked: [], confirmed_deleted: [], confirm_skipped: [], gaps: [],
      unidentified: [
        { kind: 'vm', moid: 'vm-42', name: 'stray-web', instance_uuid: 'u42', marks: ['annotation'] },
        { kind: 'vm', moid: 'vm-43', name: 'tagged', instance_uuid: 'u43', marks: ['tag'] },
        { kind: 'disk', moid: '[ds1] x/x.vmdk', name: 'x', marks: ['annotation'] },
      ],
    },
  };
}

describe('RangeInventoryComponent', () => {
  let fixture: ComponentFixture<RangeInventoryComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<RangeInventoryApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  async function render(role: UserRole, response: RangeInventoryChecks | HttpErrorResponse) {
    api = jasmine.createSpyObj('RangeInventoryApiService', ['get', 'confirmUnidentified']);
    api.get.and.returnValue(response instanceof HttpErrorResponse ? throwError(() => response) : of(response));
    api.confirmUnidentified.and.returnValue(of({
      range_id: 'r1', accepted: [], task_id: 't1', dispatched: true, message: 'Queued',
    }));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    await TestBed.configureTestingModule({
      imports: [RangeInventoryComponent, NoopAnimationsModule],
      providers: [
        { provide: RangeInventoryApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ id: 'u1', role }) } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeInventoryComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const deleteButton = () =>
    Array.from(el.querySelectorAll('button')).find(b => (b.textContent ?? '').includes('Delete')) as HTMLButtonElement;
  function typeInto(selector: string, value: string) {
    const input = el.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('shows staff the drift warning with the recorded and live values', async () => {
    await render('instructor', checks(false, 'Only an administrator (range:admin) confirms items'));
    const row = el.querySelector('[data-check="drift"] tr[data-kind="memory_changed"]');
    expect(row?.textContent).toContain('Memory changed');
    expect(row?.textContent).toContain('4096');
    expect(row?.textContent).toContain('8192');
    expect(el.querySelector('[data-check="drift"] .summary')?.getAttribute('role')).toBe('alert');
  });

  it('lists unidentified items read-only, with why, for staff who cannot confirm', async () => {
    await render('instructor', checks(false, 'Only an administrator (range:admin) confirms items'));
    expect(el.querySelectorAll('.unidentified tbody tr').length).toBe(3);
    expect(el.querySelector('input.typed')).toBeNull();
    expect(deleteButton()).toBeUndefined();
    expect(el.textContent).toContain('Only an administrator');
  });

  it('confirms only items whose id was typed back exactly, with a reason', async () => {
    await render('admin', checks(true));
    expect(el.querySelectorAll('input.typed').length).toBe(2);  // the disk file is removed by hand
    expect(deleteButton().disabled).toBeTrue();
    typeInto('tr[data-moid="vm-42"] input', 'VM-42');  // wrong case: not a match
    typeInto('tr[data-moid="vm-43"] input', 'vm-43 ');  // trailing space: not a match
    typeInto('textarea', 'stray clone left by a test, checked on vCenter');
    expect(deleteButton().disabled).toBeTrue();
    typeInto('tr[data-moid="vm-42"] input', 'vm-42');
    expect(deleteButton().disabled).toBeFalse();
    expect(deleteButton().textContent).toContain('Delete 1 confirmed');
    deleteButton().click();
    expect(api.confirmUnidentified).toHaveBeenCalledOnceWith(
      'r1', [{ moid: 'vm-42', confirm_moid: 'vm-42' }], 'stray clone left by a test, checked on vCenter');
    expect(notify.success).toHaveBeenCalledWith('Queued');
  });

  it('needs a reason of at least ten characters', async () => {
    await render('admin', checks(true));
    typeInto('tr[data-moid="vm-42"] input', 'vm-42');
    typeInto('textarea', 'short');
    expect(deleteButton().disabled).toBeTrue();
  });

  it('shows the API refusal and reloads', async () => {
    await render('admin', checks(true));
    api.confirmUnidentified.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: { message: 'Nothing was confirmed', problems: ['vm-42: not listed'] } } })));
    typeInto('tr[data-moid="vm-42"] input', 'vm-42');
    typeInto('textarea', 'stray clone left by a test, checked on vCenter');
    deleteButton().click();
    expect(notify.error).toHaveBeenCalledWith('Nothing was confirmed: vm-42: not listed');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('is not shown to Students', async () => {
    await render('student', checks(false));
    expect(api.get).not.toHaveBeenCalled();
    expect(el.textContent?.trim()).toBe('');
  });
});
