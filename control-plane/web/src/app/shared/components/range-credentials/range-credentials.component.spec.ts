import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { Clipboard } from '@angular/cdk/clipboard';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeCredentials, RangeCredentialsApiService } from '@core/services/range-credentials-api.service';
import { RangeCredentialsComponent } from './range-credentials.component';

const STAFF_VIEW: RangeCredentials = {
  range_id: 'r1', scope: 'all', accounts: [
    { machine: 'dc01', username: 'Administrator', password: 'Dc-Secret-1!', role: 'admin', student_visible: false,
      generated: false, applied: true, notes: 'Domain admin' },
    { machine: 'jump01', username: 'analyst', password: 'Jump-Secret-2!', role: 'user', student_visible: true,
      generated: true, applied: false, notes: 'The way in' },
  ],
};
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
    api = jasmine.createSpyObj('RangeCredentialsApiService', ['getCredentials']);
    api.getCredentials.and.returnValue(response instanceof HttpErrorResponse
      ? throwError(() => response) : of(response));
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

  it('fetches nothing until asked (every read is audited)', async () => {
    await render('instructor');
    expect(api.getCredentials).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Credentials');
    show();
    expect(api.getCredentials).toHaveBeenCalledOnceWith('r1');
  });

  it('shows staff every account with passwords masked until revealed one at a time', async () => {
    await render('range_ops');
    show();
    expect(el.textContent).toContain('Administrator');
    expect(el.textContent).toContain('analyst');
    expect(el.textContent).not.toContain('Dc-Secret-1!');
    expect(el.textContent).not.toContain('Jump-Secret-2!');
    expect(el.textContent).toContain('Handed out');  // staff see which accounts Students get
    button('Show password of Administrator on dc01').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Dc-Secret-1!');
    expect(el.textContent).not.toContain('Jump-Secret-2!');
    button('Hide password of Administrator on dc01').click();
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Dc-Secret-1!');
  });

  it('copies a password without revealing it', async () => {
    await render('admin');
    show();
    button('Copy password of analyst on jump01').click();
    fixture.detectChanges();
    expect(clipboard.copy).toHaveBeenCalledWith('Jump-Secret-2!');
    expect(notify.success).toHaveBeenCalledWith('Password copied');
    expect(el.textContent).not.toContain('Jump-Secret-2!');
    button('Copy username analyst on jump01').click();
    expect(clipboard.copy).toHaveBeenCalledWith('analyst');
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
    expect(api.getCredentials).not.toHaveBeenCalled();
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

  it('shows staff the template default login, and says when it is not configured', async () => {
    await render('range_ops', { range_id: 'r1', scope: 'all', accounts: [
      { machine: 'web01', username: 'ubuntu', password: 'Build-Pass-1!', source: 'template_default', role: 'admin',
        student_visible: false, generated: false, applied: false, notes: 'Template build login' },
      { machine: 'ws01', username: 'Administrator', password: null, source: 'template_default', role: 'admin',
        student_visible: false, generated: false, applied: false, notes: 'random per VM — not recoverable' },
    ] });
    show();
    const rows = Array.from(el.querySelectorAll('tbody tr'));
    expect(rows[0].textContent).toContain('Template default');
    expect(rows[0].textContent).not.toContain('Not set by TrueNorth');
    expect(rows[0].textContent).not.toContain('Build-Pass-1!');  // masked like any password
    button('Copy password of ubuntu on web01').click();
    expect(clipboard.copy).toHaveBeenCalledWith('Build-Pass-1!');
    expect(rows[1].textContent).toContain('random per VM — not recoverable');
    expect(rows[1].textContent).toContain('none shown');
    expect(button('Copy password of Administrator on ws01')).toBeUndefined();
  });

  it('says when the template declares no accounts', async () => {
    await render('instructor', { range_id: 'r1', scope: 'all', accounts: [] });
    show();
    expect(el.textContent).toContain("This range's template declares no accounts.");
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
