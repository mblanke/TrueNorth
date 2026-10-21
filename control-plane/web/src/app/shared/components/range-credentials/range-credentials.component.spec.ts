import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { Clipboard } from '@angular/cdk/clipboard';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  RangeCredential, RangeCredentials, RangeCredentialsApiService,
} from '@core/services/range-credentials-api.service';
import { RangeCredentialsComponent } from './range-credentials.component';

const BASE = { source: 'declared' as const, scenario: false, setup_status: 'not_set' as const, rotated_at: null };
const STAFF_VIEW: RangeCredentials = {
  range_id: 'r1', scope: 'all', accounts: [
    { ...BASE, id: 'c-dc', machine: 'dc01', username: 'Administrator', role: 'admin', student_visible: false,
      generated: true, applied: true, setup_status: 'applied', notes: 'Domain admin' },
    { ...BASE, id: 'c-an', machine: 'jump01', username: 'analyst', role: 'user', student_visible: true,
      generated: true, applied: false, notes: 'The way in' },
  ],
};
const PASSWORDS: Record<string, string> = { 'c-dc': 'Dc-Secret-1!', 'c-an': 'Jump-Secret-2!' };
// The API gives a Student only accounts a build set.
const STUDENT_VIEW: RangeCredentials = {
  range_id: 'r1', scope: 'student', accounts: [{ ...STAFF_VIEW.accounts[1], applied: true }],
};

describe('RangeCredentialsComponent', () => {
  let fixture: ComponentFixture<RangeCredentialsComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<RangeCredentialsApiService>;
  let clipboard: jasmine.SpyObj<Clipboard>;
  let notify: jasmine.SpyObj<NotificationService>;

  async function render(role: UserRole, response: RangeCredentials | HttpErrorResponse = STAFF_VIEW) {
    api = jasmine.createSpyObj('RangeCredentialsApiService', ['list', 'reveal', 'rotate']);
    api.list.and.returnValue(response instanceof HttpErrorResponse
      ? throwError(() => response) : of(response));
    api.reveal.and.callFake((_source, id: string) => {
      const a = (response as RangeCredentials).accounts.find(x => x.id === id) as RangeCredential;
      return of({ id, machine: a.machine, username: a.username, password: PASSWORDS[id] });
    });
    clipboard = jasmine.createSpyObj('Clipboard', ['copy']);
    clipboard.copy.and.returnValue(true);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    await TestBed.configureTestingModule({
      imports: [RangeCredentialsComponent, NoopAnimationsModule],
      providers: [
        { provide: RangeCredentialsApiService, useValue: api },
        { provide: Clipboard, useValue: clipboard },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ id: 'u1', role }) } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeCredentialsComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const button = (label: string) =>
    Array.from(el.querySelectorAll('button')).find(b =>
      (b.getAttribute('aria-label') ?? b.textContent ?? '').includes(label)) as HTMLButtonElement;

  function show() {
    button('Show accounts').click();
    fixture.detectChanges();
  }

  it('fetches nothing until asked', async () => {
    await render('instructor');
    expect(api.list).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Credentials');
    show();
    expect(api.list).toHaveBeenCalledOnceWith({ kind: 'range', rangeId: 'r1' });
    expect(api.reveal).not.toHaveBeenCalled();  // the list carries no password
  });

  it('reveals one password at a time, each an audited call, and forgets it when hidden', async () => {
    await render('range_ops');
    show();
    expect(el.textContent).toContain('Administrator');
    expect(el.textContent).toContain('analyst');
    expect(el.textContent).not.toContain('Dc-Secret-1!');
    expect(el.textContent).toContain('Handed out');  // staff see which accounts Students get
    button('Show password of Administrator on dc01').click();
    fixture.detectChanges();
    expect(api.reveal).toHaveBeenCalledOnceWith({ kind: 'range', rangeId: 'r1' }, 'c-dc');
    expect(el.textContent).toContain('Dc-Secret-1!');
    expect(el.textContent).not.toContain('Jump-Secret-2!');
    button('Hide password of Administrator on dc01').click();
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Dc-Secret-1!');
    button('Show password of Administrator on dc01').click();  // shown again: revealed again
    expect(api.reveal).toHaveBeenCalledTimes(2);
  });

  it('copies a password through a reveal, without showing it', async () => {
    await render('admin');
    show();
    button('Copy password of analyst on jump01').click();
    fixture.detectChanges();
    expect(api.reveal).toHaveBeenCalledOnceWith({ kind: 'range', rangeId: 'r1' }, 'c-an');
    expect(clipboard.copy).toHaveBeenCalledWith('Jump-Secret-2!');
    expect(notify.success).toHaveBeenCalledWith('Password copied');
    expect(el.textContent).not.toContain('Jump-Secret-2!');
    button('Copy username analyst on jump01').click();
    expect(clipboard.copy).toHaveBeenCalledWith('analyst');
  });

  it('lets an admin rotate a generated password with a reason, and nobody else', async () => {
    await render('admin');
    api.rotate.and.returnValue(of({
      account: { ...STAFF_VIEW.accounts[0], applied: false, rotated_at: '2026-10-21T12:00:00Z' },
      applies: 'in_guest', operation_id: 'op-1', dispatched: true,
    }));
    spyOn(window, 'prompt').and.returnValue('suspected exposure');
    show();
    button('Rotate password of Administrator on dc01').click();
    fixture.detectChanges();
    expect(api.rotate).toHaveBeenCalledOnceWith('r1', 'c-dc', 'suspected exposure');
    expect(notify.success).toHaveBeenCalledWith('New password issued; it is being set on the machine');
    TestBed.resetTestingModule();
    await render('instructor');
    show();
    expect(button('Rotate password of Administrator on dc01')).toBeUndefined();
  });

  it('says so when the clipboard refuses', async () => {
    await render('admin');
    show();
    clipboard.copy.and.returnValue(false);
    button('Copy password of analyst on jump01').click();
    expect(notify.error).toHaveBeenCalledWith('Password could not be copied');
  });

  it("titles a Student's panel as their own and shows only what the API returned", async () => {
    await render('student', STUDENT_VIEW);
    expect(el.textContent).toContain('Your accounts');
    show();
    expect(el.textContent).toContain('analyst');
    expect(el.textContent).not.toContain('Administrator');
    expect(el.textContent).not.toContain('Handed out');  // no staff columns
  });

  it('tells a Student who is not on the range that nothing is handed to them', async () => {
    await render('student', new HttpErrorResponse({ status: 404, error: { detail: 'Range not found' } }));
    show();
    expect(el.textContent).toContain('No accounts are handed to you on this range.');
  });

  it('is not shown to observers, whom the API refuses', async () => {
    await render('observer');
    expect(el.querySelector('section')).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  it("serves a Student's own lab through the lab endpoints, with the lab token when there is one", async () => {
    await render('student', STUDENT_VIEW);
    fixture.componentRef.setInput('labSessionId', 's1');
    fixture.componentRef.setInput('labToken', 'tok');
    fixture.detectChanges();
    show();
    expect(api.list).toHaveBeenCalledOnceWith({ kind: 'lab', sessionId: 's1', token: 'tok' });
    button('Show password of analyst on jump01').click();
    expect(api.reveal).toHaveBeenCalledOnceWith({ kind: 'lab', sessionId: 's1', token: 'tok' }, 'c-an');
    expect(button('Rotate password of analyst on jump01')).toBeUndefined();
  });

  it('flags to staff the accounts TrueNorth did not set', async () => {
    await render('range_ops');
    show();
    const rows = Array.from(el.querySelectorAll('tbody tr'));
    expect(rows[0].textContent).not.toContain('Not set by TrueNorth');  // Administrator: applied
    expect(rows[1].textContent).toContain('Not set by TrueNorth');      // analyst: not applied
  });

  it('never shows a Student the not-set badge', async () => {
    await render('student', STUDENT_VIEW);
    show();
    expect(el.textContent).not.toContain('Not set by TrueNorth');
  });

  it('shows staff a "being set up" badge for a machine whose deploy-time setup is still running', async () => {
    await render('range_ops', { range_id: 'r1', scope: 'all', accounts: [
      { ...BASE, id: 'c-ex', machine: 'exch01', username: 'Administrator', role: 'admin',
        student_visible: false, generated: true, applied: false, setup_status: 'being_set_up',
        notes: 'generated' },
    ] });
    show();
    const row = el.querySelector('tbody tr') as HTMLElement;
    expect(row.textContent).toContain('Being set up');
    expect(row.textContent).not.toContain('Not set by TrueNorth');
  });

  it('marks implicit staff logins and scenario accounts for staff; a scenario account is not rotated', async () => {
    await render('admin', { range_id: 'r1', scope: 'all', accounts: [
      { ...BASE, id: 'c-ub', machine: 'web01', username: 'ubuntu', source: 'implicit', role: 'admin',
        student_visible: false, generated: true, applied: true, notes: 'Build user of the golden template' },
      { ...BASE, id: 'c-js', machine: 'ws01', username: 'jsmith', scenario: true, role: 'user',
        student_visible: false, generated: false, applied: false, notes: 'Weak on purpose' },
    ] });
    show();
    const rows = Array.from(el.querySelectorAll('tbody tr'));
    expect(rows[0].textContent).toContain('Staff login');
    expect(rows[1].textContent).toContain('Scenario');
    expect(button('Rotate password of ubuntu on web01')).toBeTruthy();
    expect(button('Rotate password of jsmith on ws01')).toBeUndefined();
  });

  it('says when the range has no accounts', async () => {
    await render('instructor', { range_id: 'r1', scope: 'all', accounts: [] });
    show();
    expect(el.textContent).toContain('This range has no accounts (its machines are appliances).');
  });

  it('forgets the accounts when it is pointed at another range', async () => {
    await render('instructor');
    show();
    button('Show password of Administrator on dc01').click();
    fixture.componentRef.setInput('rangeId', 'r2');
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Administrator');
    expect(button('Show accounts')).toBeTruthy();
  });
});
