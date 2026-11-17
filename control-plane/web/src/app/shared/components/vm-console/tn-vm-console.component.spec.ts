import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, flushMicrotasks, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Observable, of, throwError } from 'rxjs';

import { AuthService } from '@core/services/auth.service';
import { ConsoleGrant } from './console-state';
import { FakeRfbClient, provideFakeRfb } from './fake-rfb-client';
import { TnVmConsoleComponent } from './tn-vm-console.component';

const GRANT: ConsoleGrant = { url: 'wss://tn.example/ws/console/g1', expires_in: 60, view_only: false, observed: false };

describe('TnVmConsoleComponent', () => {
  let fixture: ComponentFixture<TnVmConsoleComponent>;
  let made: FakeRfbClient[];
  let open: jasmine.Spy<() => Observable<ConsoleGrant>>;

  function make(answer: () => Observable<ConsoleGrant> = () => of(GRANT)): void {
    made = [];
    open = jasmine.createSpy('open').and.callFake(answer);
    TestBed.configureTestingModule({
      imports: [TnVmConsoleComponent, NoopAnimationsModule],
      providers: [
        provideFakeRfb(made),
        { provide: AuthService, useValue: { getToken: () => Promise.resolve('tok') } },
      ],
    });
    fixture = TestBed.createComponent(TnVmConsoleComponent);
    fixture.componentRef.setInput('machine', 'analyst01');
    fixture.componentRef.setInput('open', open);
    fixture.detectChanges();
    flushMicrotasks();
    fixture.detectChanges();
  }

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const button = (cls: string): HTMLButtonElement => el().querySelector(`button.${cls}`) as HTMLButtonElement;
  const stateLine = (): string => el().querySelector('.state')?.textContent?.trim() ?? '';
  const settle = (): void => { fixture.detectChanges(); };

  it('connects through the relay with the bearer token and counts the link down', fakeAsync(() => {
    make();
    expect(open).toHaveBeenCalledTimes(1);
    const fake = made[0];
    expect(fake.options?.url).toBe('wss://tn.example/ws/console/g1');
    expect(fake.options?.token).toBe('tok');
    expect(fake.options?.viewOnly).toBeFalse();
    expect(el().querySelector('b.name')?.textContent).toBe('analyst01');
    expect(stateLine()).toContain('Connecting');
    expect(stateLine()).toContain('60 s');
    tick(3000);
    settle();
    expect(stateLine()).toContain('57 s');
    fixture.destroy();
  }));

  it('says Connected once the screen is up and stops counting', fakeAsync(() => {
    make();
    let connected = 0;
    fixture.componentInstance.connected.subscribe(() => connected++);
    made[0].up();
    settle();
    expect(stateLine()).toBe('Connected');
    expect(connected).toBe(1);
    expect(button('cad').disabled).toBeFalse();
    expect(button('fullscreen').disabled).toBeFalse();
    tick(5000);
    expect(fixture.componentInstance.remaining()).toBeNull();
    fixture.destroy();
  }));

  it('sends Ctrl-Alt-Del only while connected', fakeAsync(() => {
    make();
    expect(button('cad').disabled).toBeTrue();
    made[0].up();
    settle();
    button('cad').click();
    expect(made[0].ctrlAltDels).toBe(1);
    fixture.destroy();
  }));

  it('is view-only when the grant says so: tagged, and no Ctrl-Alt-Del', fakeAsync(() => {
    make(() => of({ ...GRANT, view_only: true }));
    expect(made[0].options?.viewOnly).toBeTrue();
    made[0].up();
    settle();
    expect(el().querySelector('.tag')?.textContent).toContain('view only');
    expect(button('cad').disabled).toBeTrue();
    fixture.componentInstance.sendCtrlAltDel();
    expect(made[0].ctrlAltDels).toBe(0);
    fixture.destroy();
  }));

  it('tells an observer their console is observed', fakeAsync(() => {
    make(() => of({ ...GRANT, observed: true }));
    expect(el().textContent).toContain('observed');
    fixture.destroy();
  }));

  it('maps the relay\'s close code to a state and words', fakeAsync(() => {
    make();
    made[0].up();
    made[0].close(4404, 'This console was withdrawn');
    settle();
    expect(stateLine()).toBe('Closed');
    expect(el().querySelector('[role=alert]')?.textContent).toContain('not running');
    expect(el().querySelector('[role=alert]')?.textContent).toContain('This console was withdrawn');
    fixture.destroy();
  }));

  it('a clean close is Ended', fakeAsync(() => {
    make();
    made[0].up();
    made[0].close(1000, 'The console was closed');
    settle();
    expect(stateLine()).toBe('Ended');
    fixture.destroy();
  }));

  it('Reconnect asks for a fresh grant and drops the old viewer', fakeAsync(() => {
    make();
    made[0].up();
    made[0].close(4503);
    settle();
    button('reconnect').click();
    flushMicrotasks();
    settle();
    expect(open).toHaveBeenCalledTimes(2);
    expect(made.length).toBe(2);
    expect(stateLine()).toContain('Connecting');
    // The old viewer's late events no longer matter.
    made[0].close(1006);
    settle();
    expect(stateLine()).toContain('Connecting');
    fixture.destroy();
  }));

  it('a link that runs out while connecting ends the attempt', fakeAsync(() => {
    make(() => of({ ...GRANT, expires_in: 2 }));
    tick(2000);
    settle();
    expect(stateLine()).toBe('Ended');
    expect(el().querySelector('[role=alert]')?.textContent).toContain('expired');
    expect(made[0].disconnects).toBe(1);
    fixture.destroy();
    discardPeriodicTasks();
  }));

  const failures: [number, string][] = [
    [403, 'Outside your session window'],
    [409, 'not running'],
    [502, 'hypervisor refused'],
  ];
  for (const [status, words] of failures) {
    it(`says in words when the API answers ${status}`, fakeAsync(() => {
      make(() => throwError(() => new HttpErrorResponse({ status })));
      expect(stateLine()).toBe('Closed');
      expect(el().querySelector('[role=alert]')?.textContent).toContain(words);
      expect(made.length).toBe(0);
      fixture.destroy();
    }));
  }

  it('leaves the viewer when destroyed', fakeAsync(() => {
    make();
    fixture.destroy();
    expect(made[0].disconnects).toBe(1);
  }));
});
