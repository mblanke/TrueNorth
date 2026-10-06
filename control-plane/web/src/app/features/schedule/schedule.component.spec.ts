import { signal } from '@angular/core';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { Booking, CapacityResult, SchedulerApiService, Timeline } from '@core/services/scheduler-api.service';
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
  let api: jasmine.SpyObj<ApiService>;
  const user = signal<CurrentUser | null>(null);

  function as(role: CurrentUser['role'], id = 'me', bookings: Booking[] = []) {
    user.set({ sub: id, id, email: `${id}@x`, display_name: `${role} person`, role } as CurrentUser);
    scheduler.list.and.returnValue(of({ items: bookings, total: bookings.length }));
    fixture = TestBed.createComponent(ScheduleComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }
  const text = () => fixture.nativeElement.textContent as string;
  const button = (label: string) =>
    Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>).find(b => b.textContent!.trim() === label);

  beforeEach(async () => {
    scheduler = jasmine.createSpyObj('SchedulerApiService',
      ['list', 'create', 'update', 'schedule', 'cancel', 'check', 'timeline', 'getPolicy', 'setPolicy', 'feedStatus', 'issueFeed', 'revokeFeed']);
    scheduler.timeline.and.returnValue(of(TIMELINE));
    scheduler.getPolicy.and.returnValue(of({ overcapacity: 'block', can_change: true }));
    scheduler.setPolicy.and.callFake(p => of({ overcapacity: p, can_change: true }));
    scheduler.feedStatus.and.returnValue(of({ active: false, issued_at: null }));
    scheduler.check.and.returnValue(of(fits(true)));
    scheduler.create.and.callFake(body => of(booking({ id: 'new', name: body.name })));
    scheduler.schedule.and.returnValue(of(booking({})));
    scheduler.cancel.and.returnValue(of(booking({ state: 'cancelled' })));
    scheduler.issueFeed.and.returnValue(of({ url: 'https://h/api/v1/schedule/feed/T.ics', webcal_url: 'webcal://h/api/v1/schedule/feed/T.ics', issued_at: '' }));
    scheduler.revokeFeed.and.returnValue(of(void 0));
    api = jasmine.createSpyObj('ApiService', ['listTemplates', 'listRanges', 'listUsers']);
    api.listTemplates.and.returnValue(of([{ id: 'tpl-soc', name: 'SOC Training' }] as any));
    api.listRanges.and.returnValue(of([{ id: 'r1', name: 'SOC Training A', state: 'created' }, { id: 'r2', name: 'Old', state: 'destroyed' }] as any));
    api.listUsers.and.returnValue(of([{ id: 'me', display_name: 'WO Morgan Roy', role: 'instructor' }, { id: 'chen', display_name: 'Lt Sam Chen', role: 'instructor' }] as any));

    await TestBed.configureTestingModule({
      imports: [ScheduleComponent, NoopAnimationsModule],
      providers: [
        { provide: SchedulerApiService, useValue: scheduler },
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: { user, canViewSchedule: signal(true) } },
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
});
