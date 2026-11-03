import { TestBed, fakeAsync, flushMicrotasks, tick } from '@angular/core/testing';
import { signal } from '@angular/core';
import { AuthService } from './auth.service';
import { AppNotification } from './notifications-api.service';
import { UserSocketService, WEBSOCKET_FACTORY } from './user-socket.service';

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  closed = false;
  constructor(readonly url: string, readonly protocols?: string[]) {
    FakeSocket.all.push(this);
  }
  send(d: string): void { this.sent.push(d); }
  close(): void { this.closed = true; }
  // test helpers
  open(): void { this.onopen?.(); }
  receive(msg: unknown): void { this.onmessage?.({ data: typeof msg === 'string' ? msg : JSON.stringify(msg) }); }
  drop(): void { this.onclose?.(); }
}

const note = (id: string): AppNotification => ({
  id, title: `TN-${id}`, message: '', level: 'info', read: false, link: `/support/${id}`, kind: 'ticket_reply',
  created_at: '2026-10-06T12:00:00Z',
});

describe('UserSocketService (#377)', () => {
  let userId: ReturnType<typeof signal<string | null>>;
  let token = 'tok-1';

  function create(): UserSocketService {
    userId = signal<string | null>('u-1');
    FakeSocket.all = [];
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { userId, getToken: () => Promise.resolve(token) } },
        { provide: WEBSOCKET_FACTORY, useValue: (url: string, protocols?: string[]) => new FakeSocket(url, protocols) },
      ],
    });
    return TestBed.inject(UserSocketService);
  }

  const settle = () => { TestBed.flushEffects(); flushMicrotasks(); };

  it('opens the signed-in user\'s own channel with the token as a subprotocol, never in the URL', fakeAsync(() => {
    create();
    settle();
    expect(FakeSocket.all.length).toBe(1);
    expect(FakeSocket.all[0].url).toMatch(/^wss?:\/\/[^/]+\/ws\/user\.u-1$/);
    expect(FakeSocket.all[0].url).not.toContain('tok-1');
    expect(FakeSocket.all[0].protocols).toEqual(['bearer', 'tok-1']);
    TestBed.inject(UserSocketService).ngOnDestroy();
  }));

  it('sends no subprotocol when there is no token (development with sign-in off)', fakeAsync(() => {
    token = '';
    create();
    settle();
    expect(FakeSocket.all[0].protocols).toBeUndefined();
    token = 'tok-1';
    TestBed.inject(UserSocketService).ngOnDestroy();
  }));

  it('emits pushed notifications, answers pings, and ignores everything else', fakeAsync(() => {
    const svc = create();
    settle();
    const got: AppNotification[] = [];
    svc.notifications$.subscribe(n => got.push(n));
    const s = FakeSocket.all[0];
    s.open();
    s.receive({ type: 'notification', channel: 'user.u-1', data: note('a') });
    s.receive({ type: 'ping' });
    s.receive({ type: 'room_chat', data: { x: 1 } });
    s.receive({ type: 'notification', data: {} }); // no id: not a notification
    s.receive('not json');
    expect(got.map(n => n.id)).toEqual(['a']);
    expect(s.sent).toEqual([JSON.stringify({ type: 'pong' })]);
    svc.ngOnDestroy();
  }));

  it('says it is connected once open, and announces each (re)open', fakeAsync(() => {
    const svc = create();
    settle();
    let opens = 0;
    svc.opened$.subscribe(() => opens++);
    expect(svc.connected()).toBeFalse();
    FakeSocket.all[0].open();
    expect(svc.connected()).toBeTrue();
    expect(opens).toBe(1);
    svc.ngOnDestroy();
  }));

  it('reconnects with a fresh token after the server closes it, backing off', fakeAsync(() => {
    const svc = create();
    settle();
    FakeSocket.all[0].open();
    token = 'tok-2';
    FakeSocket.all[0].drop();
    expect(svc.connected()).toBeFalse();
    tick(2_000); // first back-off is about one second (+/- 25%)
    flushMicrotasks();
    expect(FakeSocket.all.length).toBe(2);
    expect(FakeSocket.all[1].protocols).toEqual(['bearer', 'tok-2']);
    // Never opened: the next try waits longer.
    FakeSocket.all[1].drop();
    tick(1_000);
    flushMicrotasks();
    expect(FakeSocket.all.length).toBe(2);
    tick(3_000);
    flushMicrotasks();
    expect(FakeSocket.all.length).toBe(3);
    token = 'tok-1';
    svc.ngOnDestroy();
  }));

  it('stops reconnecting for a user who has signed out, and closes the socket', fakeAsync(() => {
    const svc = create();
    settle();
    FakeSocket.all[0].open();
    userId.set(null);
    settle();
    expect(FakeSocket.all[0].closed).toBeTrue();
    expect(svc.connected()).toBeFalse();
    tick(120_000);
    flushMicrotasks();
    expect(FakeSocket.all.length).toBe(1);
  }));

  it('moves to the new user\'s channel when the user changes', fakeAsync(() => {
    const svc = create();
    settle();
    userId.set('u-2');
    settle();
    expect(FakeSocket.all[0].closed).toBeTrue();
    expect(FakeSocket.all[1].url).toMatch(/\/ws\/user\.u-2$/);
    svc.ngOnDestroy();
  }));
});
