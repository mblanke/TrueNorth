import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ClassLive, ClassesApiService } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ClassLiveComponent, LIVE_REFRESH_MS, observedNote } from './class-live.component';

const SEAT = (id: string) => ({
  range_id: id, name: `seat ${id}`, state: 'ready',
  machines: [{ machine: 'ws1', name: `${id}-ws1`, power: 'running' }, { machine: 'dc', name: `${id}-dc`, power: 'running' }],
});

const LIVE: ClassLive = {
  class_id: 'c1', class_name: 'C201-26-03', event_id: 'ev1', session_name: 'Mon lab',
  start_time: '2026-11-02T09:00:00Z', end_time: '2026-11-02T12:00:00Z', range_mode: 'individual',
  role: 'observer', live: true, consoles_open: true, view_only_consoles: false, shared_range: null,
  students: [
    { entry_id: 'e1', user_id: 'u1', display_name: 'Ann', team: null, range: SEAT('g1'), last_seen_at: '2026-11-02T09:05:00Z' },
    { entry_id: 'e2', user_id: 'u2', display_name: 'Bob', team: null, range: SEAT('g2'), last_seen_at: null },
  ],
  generated_at: '2026-11-02T09:10:00Z',
};

describe('observedNote', () => {
  it('tells an observer their console is observed, or view-only where the hypervisor enforces it', () => {
    expect(observedNote(LIVE)).toContain('marked observed');
    expect(observedNote({ ...LIVE, view_only_consoles: true })).toContain('view-only');
    expect(observedNote({ ...LIVE, role: 'lead' })).toBe('');
  });
});

describe('ClassLiveComponent', () => {
  let fixture: ComponentFixture<ClassLiveComponent>;
  let api: jasmine.SpyObj<ClassesApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  function make(live: ClassLive | null, error?: HttpErrorResponse): void {
    api = jasmine.createSpyObj<ClassesApiService>('ClassesApiService', ['sessionLive', 'openSessionConsole']);
    api.sessionLive.and.returnValue(error ? throwError(() => error) : of(live as ClassLive));
    api.openSessionConsole.and.returnValue(of({ range_id: 'g1', machine: 'dc', kind: 'webmks',
      url: 'wss://esx/ticket/t', expires_in: 60, view_only: false, observed: true }));
    notify = jasmine.createSpyObj('NotificationService', ['error', 'success']);
    TestBed.configureTestingModule({
      imports: [ClassLiveComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ClassesApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    });
    fixture = TestBed.createComponent(ClassLiveComponent);
    fixture.componentRef.setInput('classId', 'c1');
    fixture.componentRef.setInput('eventId', 'ev1');
    fixture.detectChanges();
  }

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;

  it('shows every Student with their machines and when they last opened their range', () => {
    make(LIVE);
    expect(api.sessionLive).toHaveBeenCalledWith('c1', 'ev1');
    const rows = el().querySelectorAll('tbody tr');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('Ann');
    expect(rows[0].textContent).toContain('ws1');
    expect(rows[1].textContent).toContain('Not yet');
    expect(el().querySelector('.observed')?.textContent).toContain('marked observed');
  });

  it('opens a console on a Student\'s machine and marks it observed', () => {
    make(LIVE);
    const dc = Array.from(el().querySelectorAll('tbody tr')[0].querySelectorAll('button'))
      .find(b => b.textContent?.includes('dc')) as HTMLButtonElement;
    dc.click();
    fixture.detectChanges();
    expect(api.openSessionConsole).toHaveBeenCalledWith('c1', 'ev1', 'g1', 'dc');
    expect(el().querySelector('.ticket')?.textContent).toContain('observed');
    expect(el().querySelector('.ticket')?.textContent).toContain('wss://esx/ticket/t');
  });

  it('outside the session window the console buttons wait', () => {
    make({ ...LIVE, live: false, consoles_open: false });
    const buttons = Array.from(el().querySelectorAll('tbody button')) as HTMLButtonElement[];
    expect(buttons.length).toBe(4);
    expect(buttons.every(b => b.disabled)).toBeTrue();
    expect(el().textContent).toContain('Consoles open during the session');
  });

  it('a collective session shows the shared range and each Student\'s team', () => {
    make({ ...LIVE, range_mode: 'collective', shared_range: SEAT('s1'),
      students: LIVE.students.map(s => ({ ...s, range: null, team: { id: 't1', name: 'Team 1' } })) });
    expect(el().querySelector('.shared')?.textContent).toContain('seat s1');
    expect(el().querySelector('tbody tr')?.textContent).toContain('Team 1');
  });

  it('a refusal (not this observer\'s class) is shown', () => {
    make(null, new HttpErrorResponse({ status: 404, error: { detail: 'Class not found' } }));
    expect(el().querySelector('.error')?.textContent).toContain('Class not found');
  });

  it('asks again while open', fakeAsync(() => {
    make(LIVE);
    expect(api.sessionLive).toHaveBeenCalledTimes(1);
    tick(LIVE_REFRESH_MS);
    expect(api.sessionLive).toHaveBeenCalledTimes(2);
    fixture.destroy();
    discardPeriodicTasks();
  }));
});
