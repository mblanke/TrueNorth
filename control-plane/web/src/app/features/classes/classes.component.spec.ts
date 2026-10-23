import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { of, throwError } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { ClassRecord, ClassesApiService, RollCallEntry } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { OperatorRecordsApiService } from '@core/services/operator-records-api.service';
import { ClassForm, ClassesComponent, addSummary, reinstateSummary, toClassBody } from './classes.component';

const CLASS: ClassRecord = {
  id: 'c1', tenant_id: 't', name: 'C201-26-03', course_id: 'k1', course_name: 'SOC Analyst',
  start_date: '2026-11-02', end_date: '2026-11-06', status: 'open',
  lead_instructor: { id: 'u1', display_name: 'Lead', email: 'lead@corp.test' },
  assistants: [], observers: [], my_role: 'lead', can_edit: true, roll_call_count: 2,
} as ClassRecord;
const ENTRIES: RollCallEntry[] = [
  { id: 'e1', email: 'joe@corp.test', user_id: null, display_name: null, status: 'invited' },
  { id: 'e2', email: 'ann@corp.test', user_id: 'u2', display_name: 'Ann', status: 'signed_in' },
  { id: 'e3', email: 'old@corp.test', user_id: null, display_name: null, status: 'removed' },
  { id: 'e4', email: 'late@corp.test', user_id: null, display_name: null, status: 'forfeited',
    forfeited_at: '2026-11-02T11:07:00Z', forfeit_deadline: '2026-11-02T11:00:00Z' },
] as RollCallEntry[];

describe('class form helpers', () => {
  const form: ClassForm = {
    name: ' C201-26-03 ', courseId: 'k1', start: '2026-11-02', end: '2026-11-06', leadId: '',
    assistantIds: [], observerIds: [],
  };

  it('sends dates as given and the caller as lead by default', () => {
    const body = toClassBody(form);
    expect(typeof body).toBe('object');
    const b = body as Exclude<typeof body, string>;
    expect(b.name).toBe('C201-26-03');
    expect(b.lead_instructor_id).toBeNull();
    expect(b.start_date).toBe('2026-11-02');
  });

  it('refuses what the API would refuse, in words', () => {
    expect(toClassBody({ ...form, name: 'x' })).toContain('name');
    expect(toClassBody({ ...form, courseId: '' })).toContain('course');
    expect(toClassBody({ ...form, end: '2026-11-01' })).toContain('on or after');
  });

  it('summarises an add', () => {
    const s = addSummary({ added: [ENTRIES[0]], already_on_roll_call: ['a@b.c'], invalid: ['x@'],
      refused: [{ email: 'i@corp.test', reason: 'staff' }],
      not_emailed: [{ email: 'n@corp.test', reason: 'daily limit' }] });
    expect(s).toContain('not emailed: n@corp.test (daily limit)');
    expect(s).toContain('Added 1');
    expect(s).toContain('already on it: 1');
    expect(s).toContain('i@corp.test (staff)');
    expect(s).toContain('x@');
  });

  it('says what a reinstatement did', () => {
    expect(reinstateSummary('a@corp.test', { entry_id: 'e', class_id: 'c', status: 'invited' }))
      .toContain('has their seat back (invited)');
    const pending = reinstateSummary('a@corp.test', {
      entry_id: 'e', class_id: 'c', status: 'pending', pending_reason: "'Lab': not enough vCPU" });
    expect(pending).toContain('an approver decides');
    expect(pending).toContain('vCPU');
  });
});

describe('ClassesComponent', () => {
  let fixture: ComponentFixture<ClassesComponent>;
  let component: ClassesComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ClassesApiService>;

  function setup(role: string, cls: ClassRecord = CLASS) {
    api = jasmine.createSpyObj('ClassesApiService',
      ['mine', 'list', 'create', 'rollCall', 'addPasted', 'addCsv', 'remove', 'reinstate']);
    api.mine.and.returnValue(of([cls]));
    api.list.and.returnValue(of([cls]));
    api.rollCall.and.returnValue(of(ENTRIES));
    api.addPasted.and.returnValue(of({ added: [ENTRIES[0]], already_on_roll_call: [], invalid: [], refused: [] }));
    api.remove.and.returnValue(of(undefined));
    api.reinstate.and.returnValue(of({ entry_id: 'e4', class_id: 'c1', status: 'invited' }));
    const records = jasmine.createSpyObj('OperatorRecordsApiService', ['listCourses', 'listUsers']);
    records.listCourses.and.returnValue(of({ items: [{ id: 'k1', name: 'SOC Analyst' }], total: 1, limit: 200, offset: 0 }));
    records.listUsers.and.returnValue(of([]));
    const notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    TestBed.configureTestingModule({
      imports: [ClassesComponent, NoopAnimationsModule],
      providers: [
        { provide: ClassesApiService, useValue: api },
        { provide: OperatorRecordsApiService, useValue: records },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ role }), isAdmin: signal(role === 'admin') } },
      ],
    });
    fixture = TestBed.createComponent(ClassesComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const buttons = (text: string) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes(text)) as HTMLButtonElement[];

  it('lists my classes and shows a roll call with statuses', () => {
    setup('instructor');
    expect(api.mine).toHaveBeenCalled();
    expect(el.textContent).toContain('C201-26-03');
    buttons('Roll call')[0].click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Invited');
    expect(el.textContent).toContain('Signed in');
    expect(el.textContent).toContain('Removed');
    expect(el.textContent).toContain('Lost seat');
    expect(buttons('Remove').filter(b => b.textContent?.trim() === 'Remove').length).toBe(3);
  });

  it('the lead reinstates a Student who lost their seat, in one click', () => {
    setup('instructor');
    component.open(CLASS);
    fixture.detectChanges();
    // The deadline they missed, not when the clock happened to notice.
    const when = el.querySelector('.when')?.textContent ?? '';
    const shown = new DatePipe('en-US').transform('2026-11-02T11:00:00Z', 'short');
    const ticked = new DatePipe('en-US').transform('2026-11-02T11:07:00Z', 'short');
    expect(when).toContain(shown!);
    expect(when).not.toContain(ticked!);
    const reinstate = buttons('Reinstate');
    expect(reinstate.length).toBe(1);  // only the forfeited entry
    reinstate[0].click();
    expect(api.reinstate).toHaveBeenCalledWith('c1', 'e4');
    expect(api.rollCall).toHaveBeenCalledTimes(2);  // the roll call is read again
  });

  it('adds pasted emails and says what happened', () => {
    setup('instructor');
    component.open(CLASS);
    component.pasted = 'joe@corp.test';
    component.addPasted();
    expect(api.addPasted).toHaveBeenCalledWith('c1', 'joe@corp.test');
    expect(component.lastAdd()).toContain('Added 1');
  });

  it('an observer reads the roll call but gets no add or remove controls', () => {
    setup('observer', { ...CLASS, my_role: 'observer', can_edit: false } as ClassRecord);
    component.open(component.classes()[0]);
    fixture.detectChanges();
    expect(el.textContent).not.toContain('Paste');
    expect(el.textContent).toContain('Lost seat');
    expect(buttons('Remove').length).toBe(0);
    expect(buttons('Reinstate').length).toBe(0);
    expect(buttons('New class').length).toBe(0);
  });

  it('an admin sees every class and may create one; a refusal is shown', () => {
    setup('admin');
    expect(api.list).toHaveBeenCalled();
    component.startCreate();
    component.patch({ name: 'C1', courseId: 'k1', start: '2026-11-02', end: '2026-11-03' });
    api.create.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: 'The lead instructor must be an active account of this organisation' } })));
    component.save();
    expect(component.formError()).toContain('lead instructor');
  });
});
