import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { ApprovalItem, Booking, SchedulerApiService, Timeline } from '@core/services/scheduler-api.service';
import { MaintenanceWindow, MaintenanceWindowsApiService } from '@core/services/maintenance-windows-api.service';
import { ClassesApiService } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { addDays, weekStart } from '../schedule/schedule.util';
import { EventsCalendarComponent } from './events-calendar.component';
import { HOUR_PX } from './events-calendar.util';

const MONDAY = weekStart(new Date());
const isoDay = (day: number) => {
  const d = addDays(MONDAY, day);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};
const at = (day: number, h: number, m = 0) => {
  const d = addDays(MONDAY, day);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};

function booking(p: Partial<Booking>): Booking {
  return {
    id: 'b', name: 'Session', state: 'scheduled', tenant_id: 't', range_id: null, template_id: 'tpl', description: null,
    instructor_id: null, created_by: null, start_time: at(0, 9), end_time: at(0, 12), vm_count: 4, vcpu_total: 20,
    ram_mb_total: 8192, disk_gb_total: 100, created_at: '', updated_at: '', ...p,
  } as Booking;
}

const CLASS_SESSION = booking({ id: 'cls', name: 'C201 Lab 1', class_id: 'c1', range_mode: 'individual', instructor_id: 'me' });
const EXERCISE = booking({
  id: 'ex', name: 'Friday exercise', class_id: 'c1', range_mode: 'collective', exercise_id: 'x1',
  start_time: at(0, 10), end_time: at(0, 11), instructor_id: 'chen',
});
const PENDING = booking({
  id: 'pend', name: 'C202 Lab 2', state: 'pending_approval', class_id: 'c2', range_mode: 'individual',
  start_time: at(1, 13), end_time: at(1, 15), vcpu_total: 30, instructor_id: 'me',
  approval: { proposed_start_time: at(2, 9), proposed_end_time: at(2, 11) } as Booking['approval'],
});
const GONE = booking({ id: 'gone', name: 'Cancelled', state: 'cancelled' });

const WINDOW: MaintenanceWindow = {
  id: 'w1', platform_wide: true, reason: 'vSphere patching', starts_at: at(2, 14), ends_at: at(2, 16), scope: 'all',
  range_ids: [], tags: [], active: false, can_edit: false,
} as MaintenanceWindow;

function timeline(): Timeline {
  return {
    cluster_vcpu: 100, cluster_ram_mb: 1_000_000, cluster_disk_gb: 100_000, supply_source: 'env',
    resolution_minutes: 15, lead_minutes: 30, grace_minutes: 15,
    buckets: [{ time: at(1, 13), vcpu_committed: 50, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 }],
  } as Timeline;
}

const QUEUE_ITEM = {
  event: PENDING, class_name: 'C202-26-04',
  fit: { fits: true, reasons: [], next_free: null, searched_until: at(6, 0) },
} as unknown as ApprovalItem;

describe('EventsCalendarComponent', () => {
  let fixture: ComponentFixture<EventsCalendarComponent>;
  let component: EventsCalendarComponent;
  let el: HTMLElement;
  let scheduler: jasmine.SpyObj<SchedulerApiService>;
  let classes: jasmine.SpyObj<ClassesApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let maintenance: jasmine.SpyObj<MaintenanceWindowsApiService>;
  const user = signal<CurrentUser | null>(null);

  function as(role: CurrentUser['role'], bookings: Booking[] = [CLASS_SESSION, EXERCISE, PENDING, GONE]) {
    user.set({ sub: 'me', id: 'me', email: 'me@x', display_name: `${role} person`, role } as CurrentUser);
    scheduler.list.and.returnValue(of({ items: bookings, total: bookings.length }));
    fixture = TestBed.createComponent(EventsCalendarComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }
  const block = (id: string) =>
    Array.from(el.querySelectorAll<HTMLButtonElement>('.week button.ev'))
      .find(b => b.getAttribute('aria-label')?.includes(id)) as HTMLButtonElement;
  /** The words on a button, without its icon's ligature. */
  const words = (e: Element) => {
    const copy = e.cloneNode(true) as Element;
    copy.querySelectorAll('mat-icon').forEach(i => i.remove());
    return copy.textContent?.trim();
  };
  const button = (label: string) =>
    Array.from(el.querySelectorAll<HTMLButtonElement>('button')).find(b => words(b) === label);
  const link = (label: string) =>
    Array.from(el.querySelectorAll<HTMLAnchorElement>('a')).find(a => a.textContent?.includes(label));

  beforeEach(async () => {
    scheduler = jasmine.createSpyObj('SchedulerApiService', [
      'list', 'timeline', 'update', 'approvals', 'approve', 'reject', 'propose', 'acceptProposal', 'declineProposal',
    ]);
    scheduler.timeline.and.returnValue(of(timeline()));
    scheduler.approvals.and.returnValue(of({ items: [QUEUE_ITEM] }));
    scheduler.approve.and.returnValue(of({ ...PENDING, state: 'scheduled' }));
    scheduler.reject.and.returnValue(of({ ...PENDING, state: 'rejected' }));
    scheduler.propose.and.returnValue(of(PENDING));
    scheduler.update.and.callFake((_id, body) => of({ ...CLASS_SESSION, start_time: body.start_time, end_time: body.end_time }));
    scheduler.acceptProposal.and.returnValue(of({ ...PENDING, state: 'scheduled' }));
    scheduler.declineProposal.and.returnValue(of(PENDING));
    maintenance = jasmine.createSpyObj('MaintenanceWindowsApiService', ['list']);
    maintenance.list.and.returnValue(of({ items: [WINDOW], total: 1 }));
    classes = jasmine.createSpyObj('ClassesApiService', ['mine', 'list', 'pendingAdditions']);
    const CLASSES = [
      { id: 'c1', name: 'C201-26-03', lead_instructor: { id: 'me', display_name: 'Me' } },
      { id: 'c2', name: 'C202-26-04', lead_instructor: { id: 'me', display_name: 'Me' } },
    ];
    classes.mine.and.returnValue(of(CLASSES as any));
    classes.list.and.returnValue(of(CLASSES as any));
    classes.pendingAdditions.and.returnValue(of([]));
    const api = jasmine.createSpyObj('ApiService', ['listUsers']);
    api.listUsers.and.returnValue(of([{ id: 'chen', display_name: 'Lt Sam Chen', role: 'instructor' }] as any));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);

    await TestBed.configureTestingModule({
      imports: [EventsCalendarComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: SchedulerApiService, useValue: scheduler },
        { provide: MaintenanceWindowsApiService, useValue: maintenance },
        { provide: ClassesApiService, useValue: classes },
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user, isAdmin: () => user()?.role === 'admin' } },
      ],
    }).compileComponents();
  });

  describe('layout', () => {
    beforeEach(() => as('instructor'));

    it('has the Events | Content switch, Today and Week | Month', () => {
      expect(el.querySelector('nav.segmented [aria-current="page"]')?.textContent).toContain('Events');
      expect(link('Content')?.getAttribute('href')).toBe('/calendar/content');
      expect(button('Today')).toBeTruthy();
      expect(el.textContent).toContain('Week');
      expect(el.textContent).toContain('Month');
    });

    it('loads the week with 15-minute capacity, and the month with hourly capacity', () => {
      const [from, to] = scheduler.list.calls.mostRecent().args;
      expect(from.getTime()).toBe(MONDAY.getTime());
      expect(to.getTime()).toBe(addDays(MONDAY, 7).getTime());
      expect(scheduler.timeline).toHaveBeenCalledWith(MONDAY, 7, 15);
      component.setView('month');
      expect(scheduler.timeline.calls.mostRecent().args[1]).toBe(42);
      expect(scheduler.timeline.calls.mostRecent().args[2]).toBe(60);
    });

    it('draws the week as a timed grid from 08:00 to 17:00, a booking placed by its time', () => {
      const labels = Array.from(el.querySelectorAll('.times span')).map(s => s.textContent?.trim());
      expect(labels[0]).toBe('08:00');
      expect(labels[labels.length - 1]).toBe('16:00');
      const b = block('C201 Lab 1');
      expect(b.style.top).toBe(`${HOUR_PX}px`);
      expect(b.style.height).toBe(`${3 * HOUR_PX}px`);
    });

    it('puts overlapping bookings side by side', () => {
      expect(block('C201 Lab 1').style.width).toBe('50%');
      expect(block('Friday exercise').style.left).toBe('50%');
    });

    it('colours classes blue with a single-person icon and exercises orange with a group icon', () => {
      const cls = block('C201 Lab 1'), ex = block('Friday exercise');
      expect(cls.classList).toContain('class');
      expect(cls.querySelector('mat-icon')?.textContent?.trim()).toBe('person');
      expect(cls.style.getPropertyValue('--c')).toBe('#1570EF');
      expect(ex.classList).toContain('exercise');
      expect(ex.querySelector('mat-icon')?.textContent?.trim()).toBe('groups');
      expect(ex.style.getPropertyValue('--c')).toBe('#EF6820');
    });

    it('outlines bookings awaiting approval as pending, and leaves cancelled ones out', () => {
      expect(block('C202 Lab 2').classList).toContain('pending');
      expect(block('C201 Lab 1').classList).not.toContain('pending');
      expect(el.querySelectorAll('.week button.ev').length).toBe(3);
    });

    it('stripes maintenance windows grey', () => {
      const band = el.querySelector('.maint') as HTMLElement;
      expect(band).toBeTruthy();
      expect(band.title).toContain('vSphere patching');
      expect(band.style.top).toBe(`${6 * HOUR_PX}px`);
    });

    it('shows each day\'s load and the extra if pending bookings were approved', () => {
      const tuesday = el.querySelectorAll('.week .head')[1];
      expect(tuesday.querySelector('.captext')?.textContent).toContain('50% · 80% if approved');
      expect((tuesday.querySelector('.extra') as HTMLElement).style.width).toBe('30%');
    });

    it('shows the month as a grid of chips', () => {
      component.setView('month');
      fixture.detectChanges();
      const chips = Array.from(el.querySelectorAll<HTMLButtonElement>('.month button.chip'));
      expect(chips.length).toBe(3);
      expect(chips.find(c => c.textContent?.includes('C202 Lab 2'))?.classList).toContain('pending');
      expect(el.querySelectorAll('.month .cell').length).toBe(42);
      expect(el.querySelector('.month .cell.maint')).toBeTruthy();
    });
  });

  describe('filters', () => {
    beforeEach(() => as('instructor'));

    const filter = (label: string) =>
      Array.from(el.querySelectorAll<HTMLButtonElement>('.legend button')).find(b => b.textContent?.includes(label))!;

    it('counts what each legend entry covers', () => {
      expect(filter('Class').textContent).toContain('2');
      expect(filter('Exercise').textContent).toContain('1');
      expect(filter('Awaiting approval').textContent).toContain('1');
      expect(filter('Maintenance').textContent).toContain('1');
    });

    it('hides exercises, pending bookings and maintenance when switched off', () => {
      filter('Exercise').click();
      fixture.detectChanges();
      expect(block('Friday exercise')).toBeUndefined();
      expect(filter('Exercise').getAttribute('aria-pressed')).toBe('false');
      filter('Awaiting approval').click();
      fixture.detectChanges();
      expect(block('C202 Lab 2')).toBeUndefined();
      filter('Maintenance').click();
      fixture.detectChanges();
      expect(el.querySelector('.maint')).toBeNull();
      expect(el.querySelectorAll('.week button.ev').length).toBe(1);
    });
  });

  describe('selected booking', () => {
    it('shows its details with Roll call, Live Class and Move for the instructor\'s own session', () => {
      as('instructor');
      block('C201 Lab 1').click();
      fixture.detectChanges();
      const side = el.querySelector('[aria-label="Selected booking"]')!;
      expect(side.textContent).toContain('C201-26-03');
      expect(side.textContent).toContain('A range for each Student');
      expect(link('Roll call')?.getAttribute('href')).toBe('/classes?class=c1');
      expect(link('Live Class')?.getAttribute('href')).toBe('/ops-center/select');
      expect(button('Move')).toBeTruthy();
    });

    it('links Live Class to the exercise when the session runs one', () => {
      as('instructor');
      block('Friday exercise').click();
      fixture.detectChanges();
      expect(link('Live Class')?.getAttribute('href')).toBe('/ops-center/x1');
      // Lt Chen is named on it, but a class session follows its class: its lead (me) moves it.
      expect(button('Move')).toBeTruthy();
      expect(el.textContent).toContain('Lt Sam Chen');
    });

    it('moves a booking: same booking, new times, through the update endpoint', () => {
      as('instructor');
      component.select(CLASS_SESSION);
      component.startMove(CLASS_SESSION);
      component.patchMove({ from: '13:00' });
      fixture.detectChanges();
      button('Save')!.click();
      const [id, body] = scheduler.update.calls.mostRecent().args;
      expect(id).toBe('cls');
      expect(new Date(body.start_time).getHours()).toBe(13);
      expect(new Date(body.end_time).getHours()).toBe(16); // same three hours
      expect(body.class_id).toBe('c1');
      expect(body.range_mode).toBe('individual');
      expect(body.template_id).toBe('tpl');
      expect(body.name).toBe('C201 Lab 1');
      expect(notify.success).toHaveBeenCalled();
    });

    it('shows the refusal when a move does not fit', () => {
      as('instructor');
      scheduler.update.and.returnValue(throwError(() => new HttpErrorResponse({
        status: 409, error: { detail: { message: 'Not enough capacity', reasons: ['vCPU short'] } } })));
      component.select(CLASS_SESSION);
      component.startMove(CLASS_SESSION);
      component.saveMove(CLASS_SESSION);
      fixture.detectChanges();
      expect(el.querySelector('[role="alert"]')?.textContent).toContain('Not enough capacity: vCPU short');
    });

    it('the instructor accepts a proposed time on their pending session', () => {
      as('instructor');
      block('C202 Lab 2').click();
      fixture.detectChanges();
      expect(el.textContent).toContain('Proposed instead');
      button('Accept')!.click();
      expect(scheduler.acceptProposal).toHaveBeenCalledWith('pend');
    });
  });

  describe('review fixes', () => {
    it('H1: a move keeps the booking\'s size, even one sized by typed totals', () => {
      const typed = booking({ id: 'typed', template_id: null, instructor_id: 'me', vm_count: 7, vcpu_total: 21,
        ram_mb_total: 30720, disk_gb_total: 500 });
      as('instructor', [typed]);
      component.select(typed);
      component.startMove(typed);
      component.saveMove(typed);
      const body = scheduler.update.calls.mostRecent().args[1];
      expect(body.template_id).toBeNull();
      expect([body.vm_count, body.vcpu_total, body.ram_mb_total, body.disk_gb_total]).toEqual([7, 21, 30720, 500]);
    });

    it('H2: moving a multi-day booking keeps its length and shows the new end', () => {
      const long = booking({ id: 'long', name: 'Long', instructor_id: 'me', start_time: at(0, 9), end_time: at(2, 17) });
      as('instructor', [long]);
      component.select(long);
      component.startMove(long);
      component.patchMove({ date: isoDay(1) });
      fixture.detectChanges();
      expect(el.querySelector('.move-end')?.textContent).toContain('17:00');
      component.saveMove(long);
      const body = scheduler.update.calls.mostRecent().args[1];
      expect(body.start_time).toBe(at(1, 9));
      expect(body.end_time).toBe(at(3, 17));
    });

    it('H2: a booking across midnight moves whole', () => {
      const late = booking({ id: 'late', name: 'Night', instructor_id: 'me', start_time: at(0, 22), end_time: at(1, 2) });
      as('instructor', [late]);
      component.select(late);
      component.startMove(late);
      component.patchMove({ date: isoDay(2), from: '21:00' });
      component.saveMove(late);
      const body = scheduler.update.calls.mostRecent().args[1];
      expect(body.start_time).toBe(at(2, 21));
      expect(body.end_time).toBe(at(3, 1));
    });

    it('M1: a maintenance window outside 08:00-17:00 is still drawn', () => {
      maintenance.list.and.returnValue(of({ items: [{ ...WINDOW, starts_at: at(2, 20), ends_at: at(2, 23) }], total: 1 }));
      as('instructor');
      const band = el.querySelector('.maint') as HTMLElement;
      expect(band).toBeTruthy();
      expect(band.style.height).toBe(`${3 * HOUR_PX}px`);
    });

    it('M2: a failed timeline reads "Load unavailable", not 0%', () => {
      scheduler.timeline.and.returnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
      as('instructor');
      const head = el.querySelector('.week .head')!;
      expect(head.textContent).toContain('Load unavailable');
      expect(head.querySelector('.cap')).toBeNull();
      expect(head.textContent).not.toContain('0%');
    });

    it('L1: an instructor named on a class session they do not lead cannot move it', () => {
      const s = booking({ id: 'other', name: 'Not mine', class_id: 'c9', range_mode: 'individual', instructor_id: 'me' });
      as('instructor', [s]);
      block('Not mine').click();
      fixture.detectChanges();
      expect(button('Move')).toBeUndefined();
    });

    it('L1: any instructor moves a booking with no instructor', () => {
      const s = booking({ id: 'free', name: 'Unassigned', instructor_id: null, created_by: 'someone' });
      as('instructor', [s]);
      block('Unassigned').click();
      fixture.detectChanges();
      expect(button('Move')).toBeTruthy();
    });

    it('L2: over capacity colours the bar high', () => {
      const tl = timeline();
      tl.buckets = [{ time: at(0, 9), vcpu_committed: 140, ram_mb_committed: 0, disk_gb_committed: 0, event_count: 1 }];
      scheduler.timeline.and.returnValue(of(tl));
      as('instructor');
      expect(el.querySelector('.week .head .used')?.classList).toContain('high');
    });

    it('L3: says so when the period has more bookings than were returned', () => {
      scheduler.list.and.returnValue(of({ items: [CLASS_SESSION], total: 250 }));
      user.set({ sub: 'me', id: 'me', email: 'me@x', display_name: 'i', role: 'instructor' } as CurrentUser);
      fixture = TestBed.createComponent(EventsCalendarComponent);
      fixture.detectChanges();
      expect(fixture.nativeElement.textContent).toContain('Showing 1 of 250 bookings');
    });

    it('L3: maintenance is reloaded on navigation and only the period\'s windows are counted', () => {
      const far = { ...WINDOW, id: 'far', starts_at: at(60, 9), ends_at: at(60, 10) };
      maintenance.list.and.returnValue(of({ items: [WINDOW, far], total: 2 }));
      as('instructor');
      expect(maintenance.list).toHaveBeenCalledWith(false, 200);
      const legend = Array.from(el.querySelectorAll('.legend button')).find(b => b.textContent?.includes('Maintenance'))!;
      expect(legend.textContent).toContain('1');
      const calls = maintenance.list.calls.count();
      component.move(-1);
      expect(maintenance.list.calls.count()).toBe(calls + 1);
      expect(maintenance.list.calls.mostRecent().args[0]).toBeTrue(); // a past week needs past windows
    });

    it('L5: a slow earlier load cannot overwrite a later one', () => {
      as('instructor');
      const first = new Subject<any>(), second = new Subject<any>();
      scheduler.list.and.returnValues(first, second);
      component.move(1);
      component.move(-1);
      second.next({ items: [EXERCISE], total: 1 }); second.complete();
      first.next({ items: [CLASS_SESSION, PENDING], total: 2 }); first.complete();
      expect(component.bookings().map(b => b.id)).toEqual(['ex']);
    });

    it('L6: load, maintenance and selection are announced', () => {
      as('instructor');
      const cap = el.querySelector('.week .head .cap')!;
      expect(cap.getAttribute('role')).toBe('img');
      expect(cap.getAttribute('aria-label')).toContain('Load');
      block('C201 Lab 1').click();
      fixture.detectChanges();
      expect(block('C201 Lab 1').getAttribute('aria-pressed')).toBe('true');
      expect(block('Friday exercise').getAttribute('aria-pressed')).toBe('false');
      component.setView('month');
      fixture.detectChanges();
      const sr = Array.from(el.querySelectorAll('.month .cell .sr-only')).map(e => e.textContent).join(' ');
      expect(sr).toContain('Maintenance: vSphere patching');
      expect(sr).toContain('Load');
    });
  });

  describe('approval queue', () => {
    it('admins see the queue and Approve calls the approve endpoint', () => {
      as('admin');
      expect(el.querySelector('tn-approval-queue')).toBeTruthy();
      expect(scheduler.approvals).toHaveBeenCalled();
      const listed = scheduler.list.calls.count();
      button('Approve')!.click();
      expect(scheduler.approve).toHaveBeenCalledWith('pend');
      expect(scheduler.list.calls.count()).toBe(listed + 1); // the calendar reloads
    });

    it('Propose time sends the chosen slot, Reject sends the reason', () => {
      as('range_ops');
      button('Propose time')!.click();
      fixture.detectChanges();
      button('Send proposal')!.click();
      expect(scheduler.propose).toHaveBeenCalledWith('pend', jasmine.any(Date), jasmine.any(Date));
      fixture.detectChanges();
      button('Reject')!.click();
      fixture.detectChanges();
      const reason = el.querySelector('tn-approval-queue input') as HTMLInputElement;
      reason.value = 'No room that week';
      reason.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      button('Reject')!.click();
      expect(scheduler.reject).toHaveBeenCalledWith('pend', 'No room that week');
    });
  });

  describe('RBAC', () => {
    it('instructors get no approval queue, but can book', () => {
      as('instructor');
      expect(el.querySelector('tn-approval-queue')).toBeNull();
      expect(scheduler.approvals).not.toHaveBeenCalled();
      expect(link('Book a session')?.getAttribute('href')).toBe('/schedule');
    });

    it('range ops approve but do not move or book, and do not load classes they cannot read', () => {
      as('range_ops');
      expect(el.querySelector('tn-approval-queue')).toBeTruthy();
      expect(link('Book a session')).toBeUndefined();
      expect(classes.mine).not.toHaveBeenCalled();
      block('C201 Lab 1').click();
      fixture.detectChanges();
      expect(button('Move')).toBeUndefined();
      expect(link('Roll call')).toBeUndefined();
    });

    it('observers read only: no queue, no Move, no Book, no Content tab', () => {
      as('observer');
      expect(el.querySelector('tn-approval-queue')).toBeNull();
      expect(link('Book a session')).toBeUndefined();
      expect(link('Content')).toBeUndefined();
      block('C201 Lab 1').click();
      fixture.detectChanges();
      expect(button('Move')).toBeUndefined();
      expect(link('Roll call')).toBeTruthy(); // class:read
      expect(link('Live Class')).toBeTruthy();
    });

    it('admins move anyone\'s booking', () => {
      as('admin');
      block('Friday exercise').click();
      fixture.detectChanges();
      expect(button('Move')).toBeTruthy();
      expect(classes.list).toHaveBeenCalled();
    });
  });
});
