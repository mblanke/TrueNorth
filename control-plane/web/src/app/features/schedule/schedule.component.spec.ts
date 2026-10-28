import { signal } from '@angular/core';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { Booking, CapacityResult, SchedulerApiService, Timeline } from '@core/services/scheduler-api.service';
import { MaintenanceWindowsApiService } from '@core/services/maintenance-windows-api.service';
import { ClassesApiService, ClassRecord } from '@core/services/classes-api.service';
import { ScheduleComponent } from './schedule.component';
import { addDays, isoDate, weekStart } from './schedule.util';

const MONDAY = weekStart(new Date());
const at = (day: number, h: number) => { const d = addDays(MONDAY, day); d.setHours(h, 0, 0, 0); return d.toISOString(); };

function booking(p: Partial<Booking>): Booking {
  return {
    id: 'b', name: 'Session', state: 'scheduled', tenant_id: 't', range_id: null, template_id: 'tpl-soc', description: null,
    instructor_id: null, created_by: null, start_time: at(0, 9), end_time: at(0, 12), vm_count: 23, vcpu_total: 69,
    ram_mb_total: 160768, disk_gb_total: 3920, created_at: '', updated_at: '', ...p,
  } as Booking;
}

const TIMELINE: Timeline = {
  buckets: [], cluster_vcpu: 108, cluster_ram_mb: 445644, cluster_disk_gb: 8704,
  supply_source: 'env', resolution_minutes: 15, lead_minutes: 30, grace_minutes: 15,
};

function fits(ok: boolean): CapacityResult {
  return {
    fits: ok, message: ok ? 'Resources available' : 'vCPU: need 69, 41 free', reasons: ok ? [] : ['vCPU: need 69, 41 free Mon 12:30–16:15 UTC'],
    vcpu_available: ok ? 108 : 41, vcpu_committed: ok ? 69 : 67, vcpu_total: 108,
    ram_mb_available: 0, ram_mb_committed: 0, ram_mb_total: 445644, disk_gb_available: 0, disk_gb_committed: 0, disk_gb_total: 8704,
    overlapping_events: 0, vm_count_needed: 23, vcpu_needed: 69, ram_mb_needed: 160768, disk_gb_needed: 3920,
    policy: 'block', supply_source: 'env',
  } as CapacityResult;
}

describe('ScheduleComponent', () => {
  let fixture: ComponentFixture<ScheduleComponent>;
  let component: ScheduleComponent;
  let scheduler: jasmine.SpyObj<SchedulerApiService>;
  let maintenance: jasmine.SpyObj<MaintenanceWindowsApiService>;
  let api: jasmine.SpyObj<ApiService>;
  let classes: jasmine.SpyObj<ClassesApiService>;
  const user = signal<CurrentUser | null>(null);

  /** The API sends what the caller may do with each booking (can_*, #319). Unless a test
   *  says otherwise, these answer as the server would for a plain booking: its instructor
   *  (or anyone, when it names none) or an admin. */
  function as(role: CurrentUser['role'], id = 'me', bookings: Booking[] = []) {
    user.set({ sub: id, id, email: `${id}@x`, display_name: `${role} person`, role } as CurrentUser);
    const items = bookings.map(b => {
      if (b.can_change !== undefined) return b;
      const may = role === 'admin' || (role === 'instructor' && (!b.instructor_id || b.instructor_id === id));
      const proposal = may && b.state === 'pending_approval' && !!b.approval?.proposed_start_time;
      return { ...b, can_change: may, can_wind_down: may && !['completed', 'cancelled', 'rejected'].includes(b.state),
               can_move: may && ['draft', 'scheduled', 'pending_approval'].includes(b.state),
               can_accept: proposal, can_decline: proposal };
    });
    scheduler.list.and.returnValue(of({ items, total: items.length }));
    fixture = TestBed.createComponent(ScheduleComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }
  const text = () => fixture.nativeElement.textContent as string;
  const button = (label: string) =>
    Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>).find(b => b.textContent!.trim() === label);

  beforeEach(async () => {
    scheduler = jasmine.createSpyObj('SchedulerApiService',
      ['list', 'create', 'update', 'schedule', 'cancel', 'check', 'timeline', 'getPolicy', 'setPolicy', 'feedStatus', 'issueFeed', 'revokeFeed', 'courses',
        'approvals', 'approve', 'reject', 'propose', 'acceptProposal', 'declineProposal']);
    scheduler.approvals.and.returnValue(of({ items: [] }));
    scheduler.acceptProposal.and.returnValue(of(booking({})));
    scheduler.declineProposal.and.returnValue(of(booking({})));
    classes = jasmine.createSpyObj('ClassesApiService', ['mine', 'list', 'pendingAdditions', 'approveAddition', 'rejectAddition']);
    classes.mine.and.returnValue(of([]));
    classes.list.and.returnValue(of([]));
    classes.pendingAdditions.and.returnValue(of([]));
    scheduler.timeline.and.returnValue(of(TIMELINE));
    scheduler.courses.and.returnValue(of({ items: [{ id: 'c204', name: 'C204 Security Monitoring' }], total: 1, limit: 200, offset: 0 } as any));
    scheduler.getPolicy.and.returnValue(of({ overcapacity: 'block', can_change: true }));
    scheduler.setPolicy.and.callFake(p => of({ overcapacity: p, can_change: true }));
    scheduler.feedStatus.and.returnValue(of({ active: false, issued_at: null }));
    scheduler.check.and.returnValue(of(fits(true)));
    scheduler.create.and.callFake(body => of(booking({ id: 'new', name: body.name })));
    scheduler.schedule.and.returnValue(of(booking({})));
    scheduler.cancel.and.returnValue(of(booking({ state: 'cancelled' })));
    scheduler.issueFeed.and.returnValue(of({ url: 'https://h/api/v1/schedule/feed/T.ics', webcal_url: 'webcal://h/api/v1/schedule/feed/T.ics', issued_at: '' }));
    scheduler.revokeFeed.and.returnValue(of(void 0));
    api = jasmine.createSpyObj('ApiService', ['listTemplates', 'listRanges', 'listUsers', 'listScenarios']);
    api.listScenarios.and.returnValue(of([{ id: 'sc1', name: 'Phishing triage' }] as any));
    api.listTemplates.and.returnValue(of([{ id: 'tpl-soc', name: 'SOC Training' }] as any));
    api.listRanges.and.returnValue(of([{ id: 'r1', name: 'SOC Training A', state: 'created' }, { id: 'r2', name: 'Old', state: 'destroyed' }] as any));
    api.listUsers.and.returnValue(of([{ id: 'me', display_name: 'WO Morgan Roy', role: 'instructor' }, { id: 'chen', display_name: 'Lt Sam Chen', role: 'instructor' }] as any));

    maintenance = jasmine.createSpyObj('MaintenanceWindowsApiService', ['active', 'list']);
    maintenance.active.and.returnValue(of([]));
    maintenance.list.and.returnValue(of({ items: [], total: 0 }));

    await TestBed.configureTestingModule({
      imports: [ScheduleComponent, NoopAnimationsModule],
      providers: [
        { provide: SchedulerApiService, useValue: scheduler },
        { provide: MaintenanceWindowsApiService, useValue: maintenance },
        { provide: ApiService, useValue: api },
        { provide: ClassesApiService, useValue: classes },
        { provide: AuthService, useValue: { user, canViewSchedule: signal(true) } },
        provideRouter([]),
      ],
    }).compileComponents();
  });

  it('lays overlapping sessions side by side', () => {
    as('observer', 'obs', [
      booking({ id: 'a', name: 'SOC lab' }),
      booking({ id: 'b', name: 'Walkthrough', start_time: at(0, 10), end_time: at(0, 12) }),
    ]);
    const evs = Array.from(fixture.nativeElement.querySelectorAll('.ev') as NodeListOf<HTMLElement>);
    expect(evs.length).toBe(2);
    expect(evs[0].style.left).not.toEqual(evs[1].style.left);
  });

  it('greets an instructor with their next session and when its range is built', () => {
    const start = new Date(Date.now() + 2 * 3_600_000);
    start.setMinutes(0, 0, 0);
    const end = new Date(start.getTime() + 3 * 3_600_000);
    as('instructor', 'me', [booking({ id: 'a', name: 'SOC lab', instructor_id: 'me', range_id: 'r1', start_time: start.toISOString(), end_time: end.toISOString() })]);
    const build = new Date(start.getTime() - 30 * 60_000);
    expect(text()).toContain('Your next session');
    expect(text()).toContain(`The range builds at ${String(build.getHours()).padStart(2, '0')}:30`);
  });

  // Issue #201: a session already under way read "Your next session … reminder the day before".
  it('shows a session already under way as live, not as the next one', () => {
    const start = new Date(Date.now() - 60_000);
    const end = new Date(Date.now() + 2 * 3_600_000);
    as('instructor', 'me', [booking({ id: 'a', name: 'SOC lab', state: 'active', instructor_id: 'me', range_id: 'r1',
      start_time: start.toISOString(), end_time: end.toISOString() })]);
    const hero: HTMLElement = fixture.nativeElement.querySelector('.hero');
    expect(hero.textContent).toContain('Live now');
    expect(hero.textContent).not.toContain('Your next session');
    expect(hero.textContent).not.toContain('reminder the day before');
    expect(hero.textContent).toContain(`until ${String(end.getHours()).padStart(2, '0')}:${String(end.getMinutes()).padStart(2, '0')}`);
    const ev: HTMLElement = fixture.nativeElement.querySelector('.ev');
    expect(ev.classList).toContain('live');
    expect(ev.textContent).toContain('Live');
  });

  it('opens the booking form on today, never on a day already past', () => {
    as('instructor');
    component.focusDay.set(0);
    component.startBooking();
    const opened = new Date(`${component.form()!.date}T00:00:00`);
    expect(opened.getTime()).toBeGreaterThanOrEqual(new Date(new Date().setHours(0, 0, 0, 0)).getTime());
  });

  it('books from a template after a live fit check', fakeAsync(() => {
    as('instructor');
    component.startBooking();
    component.patch({ name: 'Cohort 3', templateId: 'tpl-soc' });
    tick(300);
    fixture.detectChanges();
    expect(scheduler.check).toHaveBeenCalled();
    expect(text()).toContain('23 VMs · 69 vCPU');
    expect(text()).toContain('Fits.');
    button('Book it')!.click();
    const body = scheduler.create.calls.mostRecent().args[0];
    expect(body.course_id).toBeNull();
    expect(body.template_id).toBe('tpl-soc');
    expect(body.instructor_id).toBe('me');
    expect(new Date(body.start_time).getHours()).toBe(9);
  }));

  it('refuses what will not fit under Block, and books anyway under Warn', fakeAsync(() => {
    scheduler.check.and.returnValue(of(fits(false)));
    as('instructor');
    component.startBooking();
    component.patch({ name: 'Too big', templateId: 'tpl-soc' });
    tick(300);
    fixture.detectChanges();
    expect(text()).toContain('Doesn’t fit the cluster');
    expect(button('Book it')!.disabled).toBeTrue();
    component.policy.set('warn');
    fixture.detectChanges();
    expect(button('Book anyway')!.disabled).toBeFalse();
  }));

  it('previews a double booking before asking the server', fakeAsync(() => {
    as('instructor', 'me', [booking({ id: 'a', name: 'SOC lab', instructor_id: 'me' })]);
    component.startBooking();
    component.patch({ name: 'Clash', templateId: 'tpl-soc', date: isoDate(MONDAY), from: '10:00', to: '11:00' });
    tick(300);
    fixture.detectChanges();
    expect(text()).toContain('Can’t book: double-booked');
    expect(button('Book it')!.disabled).toBeTrue();
  }));

  it('books for a class, whose Students are then invited', fakeAsync(() => {
    as('instructor');
    component.startBooking();
    component.patch({ name: 'Cohort 3', templateId: 'tpl-soc', courseId: 'c204' });
    tick(300);
    fixture.detectChanges();
    expect(text()).toContain('C204 Security Monitoring');
    button('Book it')!.click();
    expect(scheduler.create.calls.mostRecent().args[0].course_id).toBe('c204');
  }));

  it('books a scenario, and links to the exercise once it exists', fakeAsync(() => {
    as('instructor', 'me', [booking({ id: 'x', instructor_id: 'me', exercise_id: 'ex1', scenario_id: 'sc1' } as any)]);
    component.startBooking();
    // Next week, clear of the fixture booking (today 09:00 would clash on a Monday).
    component.patch({ name: 'With a scenario', templateId: 'tpl-soc', scenarioId: 'sc1', date: isoDate(addDays(MONDAY, 9)), from: '13:00', to: '15:00' });
    tick(300);
    fixture.detectChanges();
    expect(text()).toContain('A range is created from the template');
    button('Book it')!.click();
    expect(scheduler.create.calls.mostRecent().args[0].scenario_id).toBe('sc1');
    component.form.set(null);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('a[href="/exercises/ex1"]')).not.toBeNull();
  }));

  it('offers only ranges that can still be built', () => {
    as('instructor');
    component.startBooking();
    fixture.detectChanges();
    const options = Array.from(fixture.nativeElement.querySelectorAll('option') as NodeListOf<HTMLOptionElement>).map(o => o.textContent);
    expect(options).toContain('SOC Training A');
    expect(options).not.toContain('Old');
  });

  it('schedules a draft and asks before cancelling', () => {
    as('instructor', 'me', [booking({ id: 'd', name: 'Draft one', state: 'draft', instructor_id: 'me' })]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    button('Schedule it')!.click();
    expect(scheduler.schedule).toHaveBeenCalledWith('d');
    fixture.detectChanges();
    button('Cancel booking')!.click();
    fixture.detectChanges();
    expect(scheduler.cancel).not.toHaveBeenCalled();
    button('Yes, cancel it')!.click();
    expect(scheduler.cancel).toHaveBeenCalledWith('d');
  });

  it('lets an instructor change only their own bookings', () => {
    as('instructor', 'me', [booking({ id: 'x', instructor_id: 'chen', created_by: 'chen' })]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(button('Move or resize')).toBeUndefined();
  });

  it('gives administrators the over-capacity policy', () => {
    as('admin');
    expect(text()).toContain('Over-capacity policy');
    (fixture.nativeElement.querySelector('input[type=radio]:not(:checked)') as HTMLInputElement).click();
    expect(scheduler.setPolicy).toHaveBeenCalledWith('warn');
  });

  it('shows a tenant administrator the policy without letting them change it', () => {
    scheduler.getPolicy.and.returnValue(of({ overcapacity: 'block', can_change: false }));
    as('admin');
    const radios = Array.from(fixture.nativeElement.querySelectorAll('input[type=radio]') as NodeListOf<HTMLInputElement>);
    expect(radios.length).toBe(2);
    expect(radios.every(r => r.disabled)).toBeTrue();
    expect(text()).toContain('platform operator');
  });

  it('links to the calendar\'s Events view (#285)', () => {
    as('instructor');
    const link = fixture.nativeElement.querySelector('a[href="/calendar/events"]') as HTMLAnchorElement;
    expect(link?.textContent).toContain('Open the calendar');
  });

  it('is read-only for observers', () => {
    as('observer', 'obs', [booking({ id: 'a' })]);
    expect(button('Book a session')).toBeUndefined();
    expect(text()).not.toContain('Over-capacity policy');
    expect(api.listUsers).not.toHaveBeenCalled();
  });

  it('shows Range Ops the load heatmap and the clock, with inherited ranges kept up', () => {
    const later = addDays(new Date(), 1);
    const iso = (d: Date, h: number) => { const x = new Date(d); x.setHours(h, 0, 0, 0); return x.toISOString(); };
    as('range_ops', 'ops', [
      booking({ id: 'a', name: 'First', range_id: 'r1', start_time: iso(later, 9), end_time: iso(later, 11) }),
      booking({ id: 'b', name: 'Second', range_id: 'r1', start_time: iso(later, 13), end_time: iso(later, 15) }),
    ]);
    expect(text()).toContain('Cluster this week');
    expect(text()).toContain('Load by hour');
    expect(text()).toContain("kept up: inherited by 'Second'");
    expect(fixture.nativeElement.querySelector('.chip.ok')?.textContent).toContain('kept');
  });

  it('shows the calendar link once, then forgets it', () => {
    as('instructor');
    button('Get calendar link')!.click();
    fixture.detectChanges();
    expect(text()).toContain('https://h/api/v1/schedule/feed/T.ics');
    button('Done')!.click();
    fixture.detectChanges();
    expect(text()).not.toContain('schedule/feed/T.ics');
    expect(text()).toContain('Regenerate link');
    button('Turn off')!.click();
    expect(scheduler.revokeFeed).toHaveBeenCalled();
  });

  // ── Class sessions and approval (ADR 0014 §14-16, #281) ─────────────
  const klass = { id: 'cls1', name: 'C201-26-03', course_id: 'c204', status: 'open', can_edit: true, my_role: 'lead' } as ClassRecord;

  it('books a class session for approval, with the class\'s course, before any capacity check', fakeAsync(() => {
    classes.mine.and.returnValue(of([klass, { ...klass, id: 'cls2', name: 'Not mine', can_edit: false } as ClassRecord]));
    scheduler.check.and.returnValue(of(fits(false)));
    scheduler.create.and.callFake(body => of(booking({ id: 'new', name: body.name, state: 'pending_approval', class_id: 'cls1' } as any)));
    as('instructor');
    component.startBooking();
    fixture.detectChanges();
    const options = Array.from(fixture.nativeElement.querySelectorAll('option') as NodeListOf<HTMLOptionElement>).map(o => o.textContent);
    expect(options).toContain('C201-26-03');
    expect(options).not.toContain('Not mine');
    component.pickClass('cls1');
    component.patch({ name: 'Lab 1', templateId: 'tpl-soc' });
    tick(400);
    fixture.detectChanges();
    expect(component.form()!.courseId).toBe('c204');
    expect(text()).toContain('Needs approval by an admin or range ops');
    const ask = button('Ask for approval')!;
    expect(ask.disabled).toBeFalse();
    ask.click();
    const body = scheduler.create.calls.mostRecent().args[0];
    expect(body.class_id).toBe('cls1');
    expect(body.course_id).toBe('c204');
    expect(body.range_mode).toBeNull();  // the course's default (ADR 0014 §9)
  }));

  it('books an individual class session: a range per Student (#282)', fakeAsync(() => {
    classes.mine.and.returnValue(of([klass]));
    scheduler.check.and.returnValue(of(fits(true)));
    scheduler.create.and.callFake(body => of(booking({ id: 'new', name: body.name, state: 'pending_approval', class_id: 'cls1' } as any)));
    as('instructor');
    component.startBooking();
    fixture.detectChanges();
    expect(text()).not.toContain('a range per Student');  // no class picked: no mode
    component.pickClass('cls1');
    component.patch({ name: 'Lab 1', templateId: 'tpl-soc', rangeMode: 'individual' });
    tick(400);
    fixture.detectChanges();
    expect(text()).toContain('Individual: a range per Student');
    button('Ask for approval')!.click();
    expect(scheduler.create.calls.mostRecent().args[0].range_mode).toBe('individual');
  }));

  it('shows the instructor a pending session and lets them accept a proposed time', () => {
    const later = new Date(at(1, 13));
    as('instructor', 'me', [booking({
      id: 'p', name: 'Lab 1', state: 'pending_approval', instructor_id: 'me', class_id: 'cls1',
      approval: { proposed_start_time: later.toISOString(), proposed_end_time: new Date(later.getTime() + 3 * 3_600_000).toISOString() },
    } as any)]);
    expect(fixture.nativeElement.querySelector('.ev.pending')).not.toBeNull();
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(text()).toContain('Awaiting approval');
    expect(text()).toContain('Proposed instead');
    button('Accept')!.click();
    expect(scheduler.acceptProposal).toHaveBeenCalledWith('p');
    button('Decline')!.click();
    expect(scheduler.declineProposal).toHaveBeenCalledWith('p');
  });

  it('#319: an assistant sees the proposal but no Accept, Decline or Move: the API says so', () => {
    const later = new Date(at(1, 13));
    as('instructor', 'me', [booking({
      id: 'p', name: 'Lab 1', state: 'pending_approval', instructor_id: 'me', class_id: 'cls1',
      approval: { proposed_start_time: later.toISOString(), proposed_end_time: new Date(later.getTime() + 3 * 3_600_000).toISOString() },
      can_change: false, can_move: false, can_accept: false, can_decline: false,
    } as any)]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(text()).toContain('Proposed instead');
    expect(button('Accept')).toBeUndefined();
    expect(button('Decline')).toBeUndefined();
    expect(button('Move or resize')).toBeUndefined();
  });

  it('#319: Cancel follows can_wind_down, not can_change (a session left under way by a cancelled class)', () => {
    as('instructor', 'me', [booking({
      id: 'w', name: 'Live lab', state: 'active', instructor_id: 'me', class_id: 'cls1',
      can_change: false, can_move: false, can_accept: false, can_decline: false, can_wind_down: true,
    } as any), booking({
      id: 'v', name: 'Not mine', state: 'active', instructor_id: 'me', class_id: 'cls2',
      can_change: false, can_move: false, can_accept: false, can_decline: false, can_wind_down: false,
    } as any)]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(button('Cancel booking')).toBeTruthy();
    expect(button('Move or resize')).toBeUndefined();
    component.select(component.bookings()[1]);
    fixture.detectChanges();
    expect(button('Cancel booking')).toBeUndefined();
  });

  it('#319: labels a class session with its class and says the roll call is reminded too', () => {
    as('instructor', 'me', [booking({ id: 's', instructor_id: 'me', class_id: 'cls1', course_id: 'c204' } as any)]);
    component.classes.set([{ id: 'cls1', name: 'C201-26-03', course_id: 'c204', status: 'open', can_edit: true } as ClassRecord]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(text()).toContain('C201-26-03');
    expect(text()).toContain('the Students on the roll call');
  });

  it('shows a rejected session with its reason', () => {
    as('instructor', 'me', [booking({ id: 'r', state: 'rejected', instructor_id: 'me', approval: { reason: 'Exercise week' } } as any)]);
    component.select(component.bookings()[0]);
    fixture.detectChanges();
    expect(text()).toContain('Rejected: Exercise week');
    expect(button('Move or resize')).toBeUndefined();
  });

  it('gives approvers the queue, and nobody else', () => {
    as('instructor');
    expect(fixture.nativeElement.querySelector('tn-approval-queue')).toBeNull();
    as('admin');
    expect(fixture.nativeElement.querySelector('tn-approval-queue')).not.toBeNull();
    as('range_ops', 'ops');
    expect(fixture.nativeElement.querySelector('tn-approval-queue')).not.toBeNull();
    expect(scheduler.approvals).toHaveBeenCalled();
  });
});
