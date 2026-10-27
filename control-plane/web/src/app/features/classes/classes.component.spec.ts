import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import {
  ClassRanges, ClassRecord, ClassesApiService, RollCallEntry, SessionTeams,
} from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { OperatorRecordsApiService } from '@core/services/operator-records-api.service';
import { ClassForm, ClassesComponent, addSummary, rangeOwner, reinstateSummary, toClassBody } from './classes.component';

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

const RANGES: ClassRanges = {
  sessions: [
    { event_id: 'ev1', name: 'Mon lab', state: 'scheduled', start_time: '2026-11-02T09:00:00Z',
      end_time: '2026-11-02T12:00:00Z', range_mode: 'individual', template_id: 't1', range_id: null },
    { event_id: 'ev2', name: 'Fri exercise', state: 'scheduled', start_time: '2026-11-06T09:00:00Z',
      end_time: '2026-11-06T12:00:00Z', range_mode: 'collective', template_id: 't2', range_id: null },
  ],
  ranges: [
    { id: 'r1', kind: 'seat', template_id: 't1', entry_id: 'e2', email: 'ann@corp.test', display_name: 'Ann',
      range_id: 'g1', status: 'ready', error: null, builds: 1 },
    { id: 'r2', kind: 'seat', template_id: 't1', entry_id: 'e1', email: 'joe@corp.test', display_name: null,
      range_id: 'g2', status: 'failed', error: 'clone failed', builds: 1 },
  ],
  failed: 1,
} as ClassRanges;
const TEAMS: SessionTeams = {
  event_id: 'ev2', editable: true,
  teams: [
    { id: 'tA', name: 'Team 1', members: [{ user_id: 'u2', display_name: 'Ann' }] },
    { id: 'tB', name: 'Team 2', members: [] },
  ],
  unassigned: [{ email: 'joe@corp.test' }],
};

describe('class range helpers', () => {
  it('names whose range a row is', () => {
    expect(rangeOwner(RANGES.ranges[0])).toBe('Ann');
    expect(rangeOwner(RANGES.ranges[1])).toBe('joe@corp.test');
    expect(rangeOwner({ ...RANGES.ranges[0], kind: 'shared' })).toContain('Shared');
  });
});

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

  function setup(role: string, cls: ClassRecord = CLASS, query: Record<string, string> = {}) {
    api = jasmine.createSpyObj('ClassesApiService', ['mine', 'list', 'create', 'rollCall', 'addPasted', 'addCsv', 'remove',
      'reinstate', 'ranges', 'retryRange', 'sessionTeams', 'splitTeams', 'moveTeamMember', 'attendance', 'closeout']);
    api.attendance.and.returnValue(of({ class_id: 'c1', class_status: 'open', can_edit: true, sessions: [], students: [] }));
    api.closeout.and.returnValue(of({ class_id: 'c1', class_status: 'open', numbers_shown: false, sessions_held: 0,
      students: [] }));
    api.ranges.and.returnValue(of(RANGES));
    api.retryRange.and.returnValue(of(RANGES.ranges[1]));
    api.sessionTeams.and.returnValue(of(TEAMS));
    api.splitTeams.and.returnValue(of(TEAMS));
    api.moveTeamMember.and.returnValue(of(TEAMS));
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
        provideHttpClient(), provideHttpClientTesting(),
        { provide: ClassesApiService, useValue: api },
        { provide: OperatorRecordsApiService, useValue: records },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ role }), isAdmin: signal(role === 'admin') } },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(query) } } },
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

  it('opens the roll call of the class named in ?class= (the calendar\'s Roll call link)', () => {
    setup('instructor', CLASS, { class: 'c1' });
    expect(api.rollCall).toHaveBeenCalledWith('c1');
    expect(component.selected()?.id).toBe('c1');
  });

  it('ignores ?class= for a class the caller cannot see', () => {
    setup('instructor', CLASS, { class: 'other' });
    expect(api.rollCall).not.toHaveBeenCalled();
    expect(component.selected()).toBeNull();
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

  it('shows the mode of each session and the status of each range; the lead retries a failed one', () => {
    setup('instructor');
    component.open(CLASS);
    fixture.detectChanges();
    expect(el.textContent).toContain('Individual: a range per Student');
    expect(el.textContent).toContain('Collective: one shared range');
    expect(el.textContent).toContain('1 range(s) failed to build');
    expect(el.textContent).toContain('clone failed');
    const retry = buttons('Retry');
    expect(retry.length).toBe(1);
    retry[0].click();
    expect(api.retryRange).toHaveBeenCalledWith('c1', 'r2');
  });

  it('an observer sees the ranges but cannot retry or change teams', () => {
    setup('observer', { ...CLASS, my_role: 'observer', can_edit: false } as ClassRecord);
    api.sessionTeams.and.returnValue(of({ ...TEAMS, editable: false }));
    component.open(component.classes()[0]);
    fixture.detectChanges();
    expect(el.textContent).toContain('Ready');
    expect(buttons('Retry').length).toBe(0);
    buttons('Teams')[0].click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Team 1');
    expect(buttons('Split').length).toBe(0);
    expect(el.querySelectorAll('.member select').length).toBe(0);
  });

  it('splits a collective session into teams and moves a Student', () => {
    setup('instructor');
    component.open(CLASS);
    fixture.detectChanges();
    expect(buttons('Teams').length).toBe(1);  // the collective session only
    buttons('Teams')[0].click();
    fixture.detectChanges();
    expect(api.sessionTeams).toHaveBeenCalledWith('c1', 'ev2');
    expect(el.textContent).toContain('Not placed (no account yet): joe@corp.test');
    component.teamCount = 3;
    buttons('Split')[0].click();
    expect(api.splitTeams).toHaveBeenCalledWith('c1', 'ev2', 3);
    component.move(CLASS, 'u2', 'tB');
    expect(api.moveTeamMember).toHaveBeenCalledWith('c1', 'ev2', 'u2', 'tB');
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
