import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { By } from '@angular/platform-browser';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { RegistrationApiService, RegistrationRequest } from '@core/services/registration-api.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { grantableRoles } from '@core/auth/permissions';
import { ApprovalsComponent } from './approvals.component';
import { ApprovalsPanelComponent } from '../users/approvals-panel.component';

const REQUEST = {
  id: 'r1', display_name: 'Cpl Avery', email: 'avery@corp.example', status: 'pending', ad_groups: '[]',
  suggested_role: 'admin', rank: null, unit: null, cohort: null,
} as unknown as RegistrationRequest;

describe('ApprovalsComponent (instructors\' registration queue)', () => {
  let registration: jasmine.SpyObj<RegistrationApiService>;

  function render(role: UserRole = 'instructor') {
    TestBed.configureTestingModule({
      imports: [ApprovalsComponent, NoopAnimationsModule],
      providers: [
        { provide: RegistrationApiService, useValue: registration },
        { provide: AuthService, useValue: fakeAuth(role) },
      ],
    });
    const fixture = TestBed.createComponent(ApprovalsComponent);
    fixture.detectChanges();
    return fixture;
  }

  beforeEach(() => {
    registration = jasmine.createSpyObj('RegistrationApiService', ['listRequests', 'approve', 'reject', 'bulkApprove']);
    registration.listRequests.and.returnValue(of([REQUEST]));
    registration.approve.and.returnValue(of(REQUEST));
  });

  it('is a page of its own around the same queue, reading GET /registration/requests', () => {
    const el: HTMLElement = render().nativeElement;
    expect(el.querySelector('h1')?.textContent).toContain('Registration approvals');
    expect(el.querySelector('tn-approvals-panel')).not.toBeNull();
    expect(registration.listRequests).toHaveBeenCalled();
    expect(el.textContent).toContain('Cpl Avery');
  });

  it('offers an instructor only the roles they may grant, and never pre-selects a staff suggestion', () => {
    const fixture = render('instructor');
    const panel: ApprovalsPanelComponent = fixture.debugElement.query(By.directive(ApprovalsPanelComponent)).componentInstance;
    expect(panel.roles).toEqual(['student', 'observer']);
    expect(panel.roleChoice['r1']).toBe('student');  // suggested 'admin' is not theirs to give
  });

  it('offers an admin every role', () => {
    expect(grantableRoles('admin')).toEqual(['student', 'observer', 'instructor', 'range_ops', 'admin']);
    expect(grantableRoles('instructor')).toEqual(['student', 'observer']);
  });

  it('says the queue failed to load instead of "Nothing waiting"', () => {
    registration.listRequests.and.returnValue(throwError(() => ({ status: 500, error: { detail: 'DB down' } })));
    const el: HTMLElement = render().nativeElement;
    expect(el.textContent).toContain('The queue could not be loaded');
    expect(el.textContent).not.toContain('Nothing waiting');
  });

  it('labels the student role "Student"', () => {
    const fixture = render('admin');
    const panel: ApprovalsPanelComponent = fixture.debugElement.query(By.directive(ApprovalsPanelComponent)).componentInstance;
    expect(panel.label('student')).toBe('Student');
  });
});
