import { Injectable, inject } from '@angular/core';
import { Observable, Subject } from 'rxjs';
import { AuthService } from './auth.service';

/** A range's new state, as the worker reported it (app/range_events.py). */
export interface RangeStateEvent {
  id: string;
  state: string;
  error: string | null;
}

/** Longest wait between reconnects. */
export const MAX_RETRY_MS = 30_000;

/**
 * Range states the worker reports, for the signed-in user's tenant, over `/ws/ranges`.
 *
 * One socket for every subscriber, opened by the first and closed with the last. The
 * access token goes as the second subprotocol (`["bearer", token]`): a browser cannot
 * set headers on a WebSocket, and a URL would put the token in proxy logs. The server
 * pings; this answers, or it is dropped. A closed socket reconnects with backoff while
 * anyone still listens. Pages keep their polling as a fallback: an event only says
 * "look again sooner".
 */
@Injectable({ providedIn: 'root' })
export class RangeEventsService {
  private readonly auth = inject(AuthService);
  private readonly events = new Subject<RangeStateEvent>();
  private socket: WebSocket | null = null;
  private listeners = 0;
  private retries = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  stream(): Observable<RangeStateEvent> {
    return new Observable<RangeStateEvent>(subscriber => {
      const sub = this.events.subscribe(subscriber);
      if (this.listeners++ === 0) void this.open();
      return () => {
        sub.unsubscribe();
        if (--this.listeners === 0) this.close();
      };
    });
  }

  /** ws(s)://<this host>/ws/ranges: nginx (and the dev server) proxy /ws to the API. */
  url(): string {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${location.host}/ws/ranges`;
  }

  private async open(): Promise<void> {
    if (this.listeners === 0 || this.socket) return;
    const token = await this.auth.getToken();
    if (this.listeners === 0 || this.socket) return;
    const socket = token ? new WebSocket(this.url(), ['bearer', token]) : new WebSocket(this.url());
    this.socket = socket;
    socket.onopen = () => { this.retries = 0; };
    socket.onmessage = (e: MessageEvent) => this.receive(socket, e.data);
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.scheduleReopen();
    };
  }

  private receive(socket: WebSocket, raw: unknown): void {
    let msg: { type?: string; data?: RangeStateEvent };
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (msg.type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
    else if (msg.type === 'range_state' && msg.data?.id) this.events.next(msg.data);
  }

  private scheduleReopen(): void {
    if (this.listeners === 0 || this.retryTimer) return;
    const wait = Math.min(MAX_RETRY_MS, 1000 * 2 ** this.retries++);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.open();
    }, wait);
  }

  private close(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }
}
