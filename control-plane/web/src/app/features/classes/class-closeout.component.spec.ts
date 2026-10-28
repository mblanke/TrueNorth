import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { AttendanceGrid, ClassRecord, ClassesApiService, Closeout } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ClassCloseoutComponent, courseStanding, draftsFrom, toResults } from './class-closeout.component';

const CLASS = {
  id: 'c1', tenant_id: 't', name: 'C201-26-03', course_id: 'k1', course_name: 'SOC Analyst',
  start_date: '2026-11-02', end_date: '2026-11-06', status: 'open', lead_instructor: null,
  assistants: [], observers: [], my_role: 'lead', can_edit: true, roll_call_count: 2,
} as unknown as ClassRecord;

const GRID: AttendanceGrid = {
  class_id: 'c1', class_status: 'open', can_edit: true,
  sessions: [{ session_id: 's1', event_id: 'ev1', name: 'Mon lab', start_time: '2026-11-02T09:00:00Z',
    end_time: '2026-11-02T12:00:00Z', ended: true }],
  students: [
    { entry_id: 'e1', email: 'ann@corp.test', display_name: 'Ann', roll_call_status: 'signed_in',
      cells: [{ session_id: 's1', status: 'present', source: 'auto' }] },
    { entry_id: 'e2', email: 'bob@corp.test', display_name: 'Bob', roll_call_status: 'signed_in',
      cells: [{ session_id: 's1', status: 'absent', source: 'auto' }] },
  ],
} as AttendanceGrid;

const CLOSEOUT: Closeout = {
  class_id: 'c1', class_status: 'open', numbers_shown: true, sessions_held: 1,
  students: [
    { entry_id: 'e1', email: 'ann@corp.test', display_name: 'Ann', sessions_held: 1, present: 1, excused: 0,
      absent: 0, course_passed: true, course_percent: 90, detections_credited: 2, suggested: 'pass' },
    { entry_id: 'e2', email: 'bob@corp.test', display_name: 'Bob', sessions_held: 1, present: 0, excused: 0,
      absent: 1, course_passed: false, course_percent: 40, detections_credited: 0, suggested: 'incomplete',
      outcome: 'fail', comment: 'Below the mark' },
  ],
} as Closeout;

describe('close-out helpers', () => {
  it('starts from the decision made, else the suggestion', () => {
    const d = draftsFrom(CLOSEOUT);
    expect(d['e1']).toEqual({ outcome: 'pass', comment: '' });
    expect(d['e2']).toEqual({ outcome: 'fail', comment: 'Below the mark' });
  });

  it('sends one result per Student with trimmed comments', () => {
    expect(toResults({ e1: { outcome: 'pass', comment: '  ' }, e2: { outcome: 'fail', comment: ' late ' } })).toEqual([
      { entry_id: 'e1', outcome: 'pass', comment: null }, { entry_id: 'e2', outcome: 'fail', comment: 'late' }]);
  });

  it('shows a percentage only where numbers are allowed', () => {
    const s = CLOSEOUT.students[0];
    expect(courseStanding(s, true)).toBe('Passed (90%)');
    expect(courseStanding({ ...s, course_percent: null }, false)).toBe('Passed');
    expect(courseStanding({ ...s, course_passed: null }, true)).toBe('No graded result yet');
  });
});

describe('ClassCloseoutComponent', () => {
  let fixture: ComponentFixture<ClassCloseoutComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ClassesApiService>;

  function setup(cls: ClassRecord, grid: AttendanceGrid, closeout: Closeout, admin = false) {
    api = jasmine.createSpyObj('ClassesApiService', ['attendance', 'closeout', 'setAttendance', 'close', 'reopen',
      'exportResults']);
    api.attendance.and.returnValue(of(grid));
    api.closeout.and.returnValue(of(closeout));
    api.setAttendance.and.returnValue(of({ session_id: 's1', status: 'excused', source: 'instructor' }));
    api.close.and.returnValue(of({ ...closeout, class_status: 'closed' }));
    api.reopen.and.returnValue(of(closeout));
    const notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    TestBed.configureTestingModule({
      imports: [ClassCloseoutComponent, NoopAnimationsModule],
      providers: [
        { provide: ClassesApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ role: admin ? 'admin' : 'instructor' }), isAdmin: signal(admin) } },
      ],
    });
    fixture = TestBed.createComponent(ClassCloseoutComponent);
    fixture.componentRef.setInput('classRecord', cls);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const buttons = (text: string) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes(text)) as HTMLButtonElement[];

  it('the lead toggles attendance per session and closes with the pre-filled results', () => {
    setup(CLASS, GRID, CLOSEOUT);
    expect(api.attendance).toHaveBeenCalledWith('c1');
    expect(el.textContent).toContain('Mon lab');
    expect(el.querySelectorAll('mat-button-toggle-group').length).toBe(2);
    // Present or excused only (ADR 0014 §12): no Absent toggle.
    const toggles = Array.from(el.querySelectorAll('mat-button-toggle')).map(t => t.textContent?.trim());
    expect(toggles).not.toContain('A');
    expect(toggles).toContain('E');
    fixture.componentInstance.note = ' medical ';
    fixture.componentInstance.setAttendance('e2', 's1', 'excused');
    expect(api.setAttendance).toHaveBeenCalledWith('c1', 's1', 'e2', 'excused', 'medical');
    expect(fixture.componentInstance.note).toBe('');
    expect(el.textContent).toContain('Passed (90%)');
    buttons('Close the class')[0].click();
    expect(api.close).toHaveBeenCalledWith('c1', [
      { entry_id: 'e1', outcome: 'pass', comment: null },
      { entry_id: 'e2', outcome: 'fail', comment: 'Below the mark' },
    ]);
  });

  it('an observer reads attendance and results without controls', () => {
    setup({ ...CLASS, my_role: 'observer', can_edit: false } as ClassRecord, { ...GRID, can_edit: false }, CLOSEOUT);
    expect(el.querySelectorAll('mat-button-toggle-group').length).toBe(0);
    expect(el.textContent).toContain('Present');
    expect(el.textContent).toContain('Absent');
    expect(buttons('Close the class').length).toBe(0);
    expect(buttons('Export CSV').length).toBe(1);
  });

  it('shows no numbers on Unclassified', () => {
    setup(CLASS, GRID, { ...CLOSEOUT, numbers_shown: false, students: CLOSEOUT.students.map(s => (
      { ...s, course_percent: null, detections_credited: null })) });
    expect(el.textContent).toContain('pass or fail only');
    expect(el.textContent).not.toContain('%');
    expect(el.textContent).not.toContain('Detections');
  });

  it('a closed class is read-only; only an admin may reopen it, with a reason', () => {
    const closed = { ...CLOSEOUT, class_status: 'closed', closed_at: '2026-11-06T17:00:00Z' } as Closeout;
    setup({ ...CLASS, status: 'closed', can_edit: false } as ClassRecord, { ...GRID, can_edit: false }, closed);
    expect(buttons('Close the class').length).toBe(0);
    expect(buttons('Reopen').length).toBe(0);
    TestBed.resetTestingModule();
    setup({ ...CLASS, status: 'closed', can_edit: false } as ClassRecord, { ...GRID, can_edit: false }, closed, true);
    const reopen = buttons('Reopen')[0];
    expect(reopen.disabled).toBeTrue();
    fixture.componentInstance.reopenReason = 'late evidence';
    fixture.componentInstance.reopen();
    expect(api.reopen).toHaveBeenCalledWith('c1', 'late evidence');
  });
});
