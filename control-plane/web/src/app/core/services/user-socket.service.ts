import { Injectable, InjectionToken, OnDestroy, effect, inject, signal } from '@angular/core';
import { Subject } from 'rxjs';
import { AuthService } from './auth.service';
import type { AppNotification } from './notifications-api.service';

/** How a socket is made; specs replace it with a fake. */
export const WEBSOCKET_FACTORY = new InjectionToken<(url: string, protocols?: string[]) => WebSocket>(
  'WEBSOCKET_FACTORY',
  { providedIn: 'root', factory: () => (url, protocols) => new WebSocket(url, protocols) },
);

const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 30_000, 60_000];
/** A socket must stay open this long before the retry counter resets: one that opens and is dropped at once (a proxy idle policy, a deploy) keeps backing off. */
const STABLE_AFTER_MS = 5_000;

/**
 * The signed-in user's own live channel, `/ws/user.<id>` (control-plane/api/app/ws_auth.py:
 * the server lets a user open only their own). Today it carries `notification` messages for
 * the bell; the bell keeps polling as the fallback, so a socket that cannot open (no `/ws/`
 * route, a proxy that drops upgrades, a refused token) costs nothing but the delay.
 *
 * The token travels as the second subprotocol, never in the URL. The server closes the socket
 * when the token expires; this reconnects with a fresh one, backing off up to a minute.
 */
@Injectable({ providedIn: 'root' })
export class UserSocketService implements OnDestroy {
  private readonly auth = inject(AuthService);
  private readonly make = inject(WEBSOCKET_FACTORY);

  /** A notification pushed to this user. */
  readonly notifications$ = new Subject<AppNotification>();
  /** Fires each time the socket opens, so a listener can catch up on anything missed while it was down. */
  readonly opened$ = new Subject<void>();
  readonly connected = signal(false);

  private socket: WebSocket | null = null;
  private forUser: string | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    // Follows sign-in and sign-out: open for the current user, close when they leave.
    effect(() => {
      const id = this.auth.userId();
      if (id === this.forUser) return;
      this.stop();
      if (id) this.start(id);
    });
  }

  ngOnDestroy(): void {
    this.stop();
  }

  private start(userId: string): void {
    this.forUser = userId;
    this.attempt = 0;
    void this.open(userId);
  }

  private stop(): void {
    this.forUser = null;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.clearStable();
    const socket = this.socket;
    this.socket = null;
    this.connected.set(false);
    if (socket) {
      socket.onclose = null;
      socket.onmessage = null;
      try { socket.close(); } catch { /* already closed */ }
    }
  }

  private async open(userId: string): Promise<void> {
    let token = '';
    try {
      token = await this.auth.getToken();
    } catch {
      return this.retry(userId);
    }
    if (this.forUser !== userId) return; // signed out (or switched) while the token loaded

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket: WebSocket;
    try {
      socket = this.make(`${proto}//${location.host}/ws/user.${userId}`, token ? ['bearer', token] : undefined);
    } catch {
      return this.retry(userId);
    }
    this.socket = socket;
    socket.onopen = () => {
      this.connected.set(true);
      this.opened$.next();
      this.clearStable();
      this.stableTimer = setTimeout(() => (this.attempt = 0), STABLE_AFTER_MS);
    };
    socket.onmessage = ev => this.onMessage(socket, ev.data);
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.connected.set(false);
      this.clearStable();
      this.retry(userId);
    };
  }

  private onMessage(socket: WebSocket, raw: unknown): void {
    let msg: { type?: string; data?: AppNotification } | null = null;
    try {
      msg = typeof raw === 'string' ? JSON.parse(raw) : null;
    } catch {
      return;
    }
    if (!msg) return;
    if (msg.type === 'ping') {
      socket.send(JSON.stringify({ type: 'pong' })); // unanswered pings drop the socket
    } else if (msg.type === 'notification' && msg.data?.id) {
      this.notifications$.next(msg.data);
    }
  }

  private clearStable(): void {
    if (this.stableTimer) clearTimeout(this.stableTimer);
    this.stableTimer = null;
  }

  private retry(userId: string): void {
    if (this.forUser !== userId) return;
    const base = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    this.timer = setTimeout(() => void this.open(userId), base * (0.75 + Math.random() * 0.5));
  }
}
