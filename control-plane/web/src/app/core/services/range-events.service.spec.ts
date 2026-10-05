import { TestBed, fakeAsync, flushMicrotasks, tick } from '@angular/core/testing';
import { AuthService } from './auth.service';
import { RangeEventsService, RangeStateEvent } from './range-events.service';

/** Stands in for the browser's WebSocket; the latest one is FakeSocket.last. */
class FakeSocket {
  static last: FakeSocket | null = null;
  static opened = 0;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string, public protocols?: string[]) {
    FakeSocket.last = this;
    FakeSocket.opened++;
  }
  send(data: string): void { this.sent.push(data); }
  close(): void { this.closed = true; }
  emit(msg: object): void { this.onmessage?.({ data: JSON.stringify(msg) }); }
}

describe('RangeEventsService', () => {
  let service: RangeEventsService;
  let token = '';
  const real = window.WebSocket;

  beforeEach(() => {
    FakeSocket.last = null;
    FakeSocket.opened = 0;
    (window as unknown as { WebSocket: unknown }).WebSocket = FakeSocket;
    TestBed.configureTestingModule({
      providers: [{ provide: AuthService, useValue: { getToken: () => Promise.resolve(token) } }],
    });
    service = TestBed.inject(RangeEventsService);
  });

  afterEach(() => {
    (window as unknown as { WebSocket: unknown }).WebSocket = real;
  });

  it('sends the token as a subprotocol, never in the URL', fakeAsync(() => {
    token = 'jwt.abc.def';
    const sub = service.stream().subscribe();
    flushMicrotasks();
    expect(FakeSocket.last?.protocols).toEqual(['bearer', 'jwt.abc.def']);
    expect(FakeSocket.last?.url).toMatch(/\/ws\/ranges$/);
    expect(FakeSocket.last?.url).not.toContain('jwt');
    sub.unsubscribe();
    token = '';
  }));

  it('emits range states and answers pings', fakeAsync(() => {
    const got: RangeStateEvent[] = [];
    const sub = service.stream().subscribe(e => got.push(e));
    flushMicrotasks();
    const socket = FakeSocket.last!;
    socket.emit({ type: 'ping' });
    socket.emit({ type: 'range_state', data: { id: 'r1', state: 'ready', error: null } });
    socket.emit({ type: 'something_else', data: { id: 'r2' } });
    expect(socket.sent).toEqual([JSON.stringify({ type: 'pong' })]);
    expect(got).toEqual([{ id: 'r1', state: 'ready', error: null }]);
    sub.unsubscribe();
  }));

  it('shares one socket and closes it with the last subscriber', fakeAsync(() => {
    const a = service.stream().subscribe();
    const b = service.stream().subscribe();
    flushMicrotasks();
    expect(FakeSocket.opened).toBe(1);
    a.unsubscribe();
    expect(FakeSocket.last!.closed).toBeFalse();
    b.unsubscribe();
    expect(FakeSocket.last!.closed).toBeTrue();
  }));

  it('reconnects after the socket drops while someone listens', fakeAsync(() => {
    const sub = service.stream().subscribe();
    flushMicrotasks();
    FakeSocket.last!.onclose?.();
    tick(1000);
    flushMicrotasks();
    expect(FakeSocket.opened).toBe(2);
    sub.unsubscribe();
  }));
});
