import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { fakeAuth } from '@core/auth/fake-auth';
import { AuthService } from '@core/services/auth.service';
import { ClassesApiService, MyClass, MyClasses } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { RangeCredentialsApiService } from '@core/services/range-credentials-api.service';
import { addDays } from '../calendar/content-calendar.util';
import { MyClassesComponent, REFRESH_MS, forfeitMessage, joinHint, rangeLabel, windowOf } from './my-classes.component';

const BASE: MyClass = {
  id: 'c1', name: 'C201-26-03', course_name: 'SOC Analyst', lead_instructor: 'Lee Lead',
  start_date: '2026-11-02', end_date: '2026-11-06', onboarded: true, seat: 'ready',
  forfeit_deadline: null, forfeited_at: null,
  sessions: [
    { event_id: 'ev1', name: 'Mon lab', start_time: '2026-11-02T09:00:00Z', end_time: '2026-11-02T12:00:00Z',
      range_mode: 'individual', live: true, range_status: 'ready' },
    { event_id: 'ev2', name: 'Fri exercise', start_time: '2026-11-06T09:00:00Z', end_time: '2026-11-06T12:00:00Z',
      range_mode: 'collective', live: false, range_status: 'not_built' },
  ],
  range_status: 'ready', next_session_id: 'ev1', join_range_id: 'rng-1',
};
const FORFEITED: MyClass = {
  ...BASE, id: 'c2', name: 'C202-26-04', onboarded: false, seat: 'forfeited',
  forfeit_deadline: '2026-11-02T11:00:00Z', forfeited_at: '2026-11-02T11:01:00Z',
  range_status: null, join_range_id: null,
  sessions: BASE.sessions.map(s => ({ ...s, range_status: null })),
};
const BUILDING: MyClass = { ...BASE, id: 'c3', name: 'C203-26-05', range_status: 'building', join_range_id: null };

function page(classes: MyClass[], signed = true): MyClasses {
  return { acknowledgement_signed: signed, window_start: '2026-11-02T00:00:00Z', window_end: '2026-11-09T00:00:00Z',
    classes };
}

describe('my classes helpers', () => {
  it('a week window runs Monday to Monday; a month window covers the month', () => {
    const wk = windowOf(new Date(2026, 9, 28), 'week');  // a Wednesday
    expect(wk.start).toEqual(new Date(2026, 9, 26));
    expect(wk.end).toEqual(new Date(2026, 10, 2));
    const mo = windowOf(new Date(2026, 9, 28), 'month');
    expect(mo.start).toEqual(new Date(2026, 9, 1));
    expect(mo.end).toEqual(new Date(2026, 10, 1));
  });

  it('labels every range status', () => {
    expect(rangeLabel('not_built')).toBe('Range not built');
    expect(rangeLabel('building')).toBe('Range building');
    expect(rangeLabel('ready')).toBe('Range ready');
    expect(rangeLabel('failed')).toBe('Range failed');
    expect(rangeLabel('paused')).toBe('Range paused');
    expect(rangeLabel(null)).toBe('No range');
  });

  it('says why Join is not live', () => {
    expect(joinHint(BASE)).toBe('');
    expect(joinHint(BUILDING)).toContain('being built');
    expect(joinHint({ ...BASE, join_range_id: null, range_status: 'not_built' })).toContain('before your session');
    expect(joinHint({ ...BASE, join_range_id: null, range_status: 'failed' })).toContain('instructor');
    expect(joinHint({ ...BASE, join_range_id: null, range_status: 'ready' })).toContain('when the session starts');
    expect(joinHint({ ...BASE, join_range_id: null, range_status: null })).toContain('No session');
    expect(joinHint(FORFEITED)).toContain('reinstate');
  });

  it('tells a Student who lost their seat whom to ask', () => {
    const msg = forfeitMessage(FORFEITED);
    expect(msg).toContain('You lost your seat');
    expect(msg).toContain('Lee Lead');
    expect(msg).toContain('reinstate');
    expect(forfeitMessage({ ...FORFEITED, lead_instructor: null })).toContain('your instructor');
    expect(forfeitMessage(BASE)).toBe('');
  });
});

describe('MyClassesComponent', () => {
  let fixture: ComponentFixture<MyClassesComponent>;
  let api: jasmine.SpyObj<ClassesApiService>;
  let creds: jasmine.SpyObj<RangeCredentialsApiService>;

  function make(data: MyClasses): void {
    api = jasmine.createSpyObj<ClassesApiService>('ClassesApiService', ['attending']);
    api.attending.and.returnValue(of(data));
    creds = jasmine.createSpyObj<RangeCredentialsApiService>('RangeCredentialsApiService', ['list', 'reveal']);
    TestBed.configureTestingModule({
      imports: [MyClassesComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: AuthService, useValue: fakeAuth('student') },
        { provide: ClassesApiService, useValue: api },
        { provide: RangeCredentialsApiService, useValue: creds },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['error', 'success']) },
      ],
    });
    fixture = TestBed.createComponent(MyClassesComponent);
    fixture.detectChanges();
  }

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const card = (id: string): HTMLElement => el().querySelector(`[data-class="${id}"]`) as HTMLElement;
  const join = (id: string): HTMLButtonElement => card(id).querySelector('button.join') as HTMLButtonElement;

  it('lists each class with its course, lead, dates and sessions', () => {
    make(page([BASE]));
    const c = card('c1');
    expect(c.textContent).toContain('C201-26-03');
    expect(c.textContent).toContain('SOC Analyst');
    expect(c.textContent).toContain('Lee Lead');
    const sessions = c.querySelectorAll('.session');
    expect(sessions.length).toBe(2);
    expect(sessions[0].textContent).toContain('Individual');
    expect(sessions[0].textContent).toContain('Live now');
    expect(sessions[1].textContent).toContain('Collective');
  });

  it('shows the status chips: onboarding, rules of use, seat and range', () => {
    make(page([BASE, BUILDING]));
    const chips = (id: string) => Array.from(card(id).querySelectorAll('.status .chip')).map(e => e.textContent?.trim());
    expect(chips('c1')).toEqual(['Onboarded', 'Rules of use signed', 'Seat ready', 'Range ready']);
    expect(chips('c3')).toContain('Range building');
  });

  it('shows rules of use as not signed when the API says so', () => {
    make(page([BASE], false));
    const chips = Array.from(card('c1').querySelectorAll('.status .chip')).map(e => e.textContent?.trim());
    expect(chips).toContain('Rules of use not signed');
  });

  it('Join is live when the range is ready, and opens the Student\'s range accounts', () => {
    make(page([BASE]));
    expect(join('c1').disabled).toBeFalse();
    expect(card('c1').querySelector('tn-range-credentials')).toBeNull();
    join('c1').click();
    fixture.detectChanges();
    expect(card('c1').querySelector('tn-range-credentials')).not.toBeNull();
    expect(card('c1').textContent).toContain('Your range');
  });

  it('Join is disabled, with the reason, when the range is not ready', () => {
    make(page([BUILDING]));
    expect(join('c3').disabled).toBeTrue();
    expect(card('c3').querySelector('.hint')?.textContent).toContain('being built');
  });

  it('a powered-off range is paused, and Join waits', () => {
    make(page([{ ...BASE, range_status: 'paused', join_range_id: null }]));
    const chips = Array.from(card('c1').querySelectorAll('.status .chip')).map(e => e.textContent?.trim());
    expect(chips).toContain('Range paused');
    expect(join('c1').disabled).toBeTrue();
    expect(card('c1').querySelector('.hint')?.textContent).toContain('powered off');
  });

  it('a lost seat says so, names whom to ask, and offers no Join', () => {
    make(page([FORFEITED]));
    const c = card('c2');
    expect(c.querySelector('.forfeit')?.textContent).toContain('You lost your seat');
    expect(c.querySelector('.forfeit')?.textContent).toContain('Lee Lead');
    const chips = Array.from(c.querySelectorAll('.status .chip')).map(e => e.textContent?.trim());
    expect(chips).toContain('Lost seat');
    expect(chips).toContain('Not onboarded');
    expect(join('c2').disabled).toBeTrue();
  });

  it('starts on the week, and the toggle asks for the month', () => {
    make(page([BASE]));
    const [s1, e1] = api.attending.calls.mostRecent().args;
    expect(e1).toEqual(addDays(s1, 7));
    expect(s1.getDay()).toBe(1);  // a Monday
    const month = Array.from(el().querySelectorAll('mat-button-toggle button'))
      .find(b => b.textContent?.includes('Month')) as HTMLButtonElement;
    month.click();
    fixture.detectChanges();
    const [s2, e2] = api.attending.calls.mostRecent().args;
    expect(s2.getDate()).toBe(1);
    expect(e2.getDate()).toBe(1);
    expect(e2.getMonth()).toBe((s2.getMonth() + 1) % 12);
    expect(fixture.componentInstance.view()).toBe('month');
  });

  it('moves a period at a time and back to today', () => {
    make(page([BASE]));
    const first = api.attending.calls.mostRecent().args[0];
    (el().querySelector('[aria-label="Next week"]') as HTMLButtonElement).click();
    expect(api.attending.calls.mostRecent().args[0]).toEqual(addDays(first, 7));  // local days: DST-proof
    (el().querySelector('button.today') as HTMLButtonElement).click();
    expect(api.attending.calls.mostRecent().args[0]).toEqual(first);
  });

  it('asks again every minute, so Join goes live without a reload', fakeAsync(() => {
    make(page([BUILDING]));
    expect(join('c3').disabled).toBeTrue();
    api.attending.and.returnValue(of(page([{ ...BUILDING, range_status: 'ready', join_range_id: 'rng-3' }])));
    tick(REFRESH_MS);
    fixture.detectChanges();
    expect(join('c3').disabled).toBeFalse();
    fixture.destroy();
    discardPeriodicTasks();
  }));

  it('shows the empty state when there is nothing in the window', () => {
    make(page([]));
    expect(el().querySelector('tn-empty-state')).not.toBeNull();
  });
});
