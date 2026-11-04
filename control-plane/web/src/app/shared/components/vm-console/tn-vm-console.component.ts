import {
  ChangeDetectionStrategy, Component, DestroyRef, ElementRef, effect, inject, input, output, signal, untracked,
  viewChild,
} from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { Observable, Subscription } from 'rxjs';

import { AuthService } from '@core/services/auth.service';
import {
  ConsoleGrant, ConsoleNotice, ConsoleStatus, closeNotice, expiredNotice, openErrorText,
} from './console-state';
import { RFB_CLIENT_FACTORY, RfbClient } from './rfb-client';

/** A relay URL as given: absolute wss://..., or a path on this site. */
function absoluteUrl(url: string): string {
  if (/^wss?:\/\//i.test(url)) return url;
  const base = new URL(url, window.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  return base.toString();
}

/**
 * A machine's console, drawn in the page (ADR 0014, #368). The parent gives it `open`, a
 * function that asks the API for a console grant; the component calls it on creation and on
 * every Reconnect, so a Reconnect always carries a fresh grant. It connects through
 * TrueNorth's relay only (`wss://<tn>/ws/console/<grant>`), never to the hypervisor.
 *
 * The RFB viewer sits behind `RfbClient` (noVNC by default, loaded when first needed).
 */
@Component({
  selector: 'tn-vm-console',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatIconModule],
  template: `
    <section class="console" [attr.aria-label]="'Console: ' + machine()" [attr.data-status]="status()">
      <div class="toolbar">
        <b class="name">{{ machine() }}</b>
        <span class="state" role="status">
          <i class="dot" [attr.data-status]="status()"></i>{{ stateLine() }}
        </span>
        @if (viewOnly()) { <span class="tag">view only</span> }
        @if (observed()) { <span class="tag">observed</span> }
        <span class="grow"></span>
        <button mat-stroked-button type="button" class="cad" (click)="sendCtrlAltDel()" [disabled]="!canSend()">
          Send Ctrl-Alt-Del
        </button>
        <button mat-stroked-button type="button" class="reconnect" (click)="connect()">
          <mat-icon>refresh</mat-icon> Reconnect
        </button>
        <button mat-stroked-button type="button" class="fullscreen" (click)="fullScreen()"
                [disabled]="status() !== 'connected'">
          <mat-icon>fullscreen</mat-icon> Full screen
        </button>
      </div>
      @if (notice(); as n) {
        <p class="problem" [class.ended]="n.status === 'ended'" role="alert">
          {{ n.text }}
          @if (n.detail) { <span class="detail">({{ n.detail }})</span> }
        </p>
      }
      <div class="screen" #box>
        <div class="viewer" #viewer></div>
        @if (status() !== 'connected') {
          <p class="placeholder">
            @if (status() === 'connecting') { Connecting to {{ machine() }}… } @else { No picture. }
          </p>
        }
      </div>
      @if (viewOnly()) {
        <p class="hint">View only: your keyboard and mouse are not sent to the machine.</p>
      } @else if (observed()) {
        <p class="hint">Observed: this hypervisor cannot make the console view-only, so do not type into a Student's machine.</p>
      }
    </section>
  `,
  styles: [`
    :host { display: block; }
    .console { background: var(--tn-surface, #fff); border: 1px solid var(--tn-line, #eadbdd); border-radius: 10px;
      padding: 14px 16px; color: var(--tn-ink, #292326); }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .grow { flex: 1; }
    .name { font-size: 18px; }
    .state { color: var(--tn-muted, #655b60); display: inline-flex; align-items: center; }
    .dot { width: 9px; height: 9px; border-radius: 5px; margin-right: 6px; background: var(--tn-line, #eadbdd); display: inline-block; }
    .dot[data-status='connected'] { background: var(--tn-ok, #2f6b1f); }
    .dot[data-status='connecting'] { background: var(--tn-warm-ink, #7a4a06); }
    .dot[data-status='closed'] { background: var(--tn-accent, #b5122b); }
    .tag { font-size: 13px; padding: 2px 8px; border-radius: 6px; background: var(--tn-soft, #fcecef); color: var(--tn-accent, #b5122b); }
    .problem { margin: 10px 0 0; padding: 8px 12px; border-radius: 8px; background: var(--tn-soft, #fcecef);
      color: var(--tn-accent, #b5122b); }
    .problem.ended { background: var(--tn-warm, #fbeed8); color: var(--tn-warm-ink, #7a4a06); }
    .detail { color: var(--tn-muted, #655b60); }
    .screen { position: relative; margin-top: 12px; min-height: 320px; border: 2px solid var(--tn-line, #eadbdd);
      border-radius: 10px; background: #fafafa; overflow: hidden; display: flex; }
    .viewer { flex: 1; min-width: 0; min-height: 320px; }
    .placeholder { position: absolute; inset: 0; margin: 0; display: flex; align-items: center; justify-content: center;
      text-align: center; padding: 30px; color: var(--tn-muted, #655b60); pointer-events: none; }
    .hint { margin: 10px 0 0; font-size: 14px; color: var(--tn-muted, #655b60); }
  `],
})
export class TnVmConsoleComponent {
  private readonly auth = inject(AuthService);
  private readonly makeClient = inject(RFB_CLIENT_FACTORY);

  /** The machine's name, for the toolbar. */
  readonly machine = input.required<string>();
  /** Asks the API for a console on this machine. Called on creation, when it changes, and on Reconnect. */
  readonly open = input.required<() => Observable<ConsoleGrant>>();
  /** The remote screen came up: the parent can count the console as activity. */
  readonly connected = output<void>();
  readonly statusChange = output<ConsoleStatus>();

  readonly status = signal<ConsoleStatus>('idle');
  readonly notice = signal<ConsoleNotice | null>(null);
  /** Seconds the grant has left, while connecting. */
  readonly remaining = signal<number | null>(null);
  readonly viewOnly = signal(false);
  readonly observed = signal(false);

  private readonly viewer = viewChild.required<ElementRef<HTMLElement>>('viewer');
  private readonly box = viewChild.required<ElementRef<HTMLElement>>('box');
  private client: RfbClient | null = null;
  private request: Subscription | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Bumped by each connect, so a late answer of an earlier one is dropped. */
  private run = 0;

  constructor() {
    // Opens on creation and again when the parent hands over a new `open` (another machine).
    effect(() => {
      this.open();
      untracked(() => this.connect());
    });
    inject(DestroyRef).onDestroy(() => this.teardown());
  }

  /** The state line: connecting / connected / ended / closed. */
  stateLine(): string {
    switch (this.status()) {
      case 'connecting': {
        const left = this.remaining();
        return left === null ? 'Connecting' : `Connecting · link good for ${left} s`;
      }
      case 'connected': return 'Connected';
      case 'ended': return 'Ended';
      case 'closed': return 'Closed';
      default: return 'Not connected';
    }
  }

  canSend(): boolean {
    return this.status() === 'connected' && !this.viewOnly();
  }

  /** Asks for a fresh grant and connects with it. */
  connect(): void {
    this.teardown();
    const run = ++this.run;
    this.notice.set(null);
    this.remaining.set(null);
    this.setStatus('connecting');
    this.request = this.open()().subscribe({
      next: grant => void this.start(grant, run),
      error: err => {
        if (run !== this.run) return;
        this.finish({ status: 'closed', text: openErrorText(err), detail: '' });
      },
    });
  }

  sendCtrlAltDel(): void {
    if (this.canSend()) this.client?.sendCtrlAltDel();
  }

  fullScreen(): void {
    void this.box().nativeElement.requestFullscreen?.()?.catch(() => undefined);
  }

  private async start(grant: ConsoleGrant, run: number): Promise<void> {
    this.viewOnly.set(!!grant.view_only);
    this.observed.set(!!grant.observed);
    this.countdown(grant.expires_in, run);
    try {
      const [client, token] = await Promise.all([this.makeClient(), this.auth.getToken()]);
      if (run !== this.run) return;
      this.client = client;
      client.connect(
        { target: this.viewer().nativeElement, url: absoluteUrl(grant.url), token, viewOnly: !!grant.view_only },
        {
          connected: () => this.onConnected(run),
          closed: (code, reason) => this.onClosed(run, code, reason),
        });
    } catch {
      if (run !== this.run) return;
      this.finish({ status: 'closed', text: 'The console viewer could not be started. Press Reconnect.', detail: '' });
    }
  }

  private onConnected(run: number): void {
    if (run !== this.run) return;
    this.stopTimer();
    this.remaining.set(null);
    this.setStatus('connected');
    this.connected.emit();
  }

  private onClosed(run: number, code: number, reason: string): void {
    if (run !== this.run) return;
    this.finish(closeNotice(code, reason));
  }

  /** Counts the grant down while connecting; a link that runs out first ends the attempt. */
  private countdown(seconds: number, run: number): void {
    this.stopTimer();
    this.remaining.set(Math.max(0, Math.floor(seconds)));
    this.timer = setInterval(() => {
      if (run !== this.run || this.status() !== 'connecting') return this.stopTimer();
      const left = Math.max(0, (this.remaining() ?? 0) - 1);
      this.remaining.set(left);
      if (left === 0) this.finish(expiredNotice());
    }, 1000);
  }

  private finish(notice: ConsoleNotice): void {
    this.teardown();
    this.remaining.set(null);
    this.notice.set(notice);
    this.setStatus(notice.status);
  }

  private setStatus(status: ConsoleStatus): void {
    if (this.status() === status) return;
    this.status.set(status);
    this.statusChange.emit(status);
  }

  private stopTimer(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  private teardown(): void {
    // Whatever is still in flight (an answer, the viewer's own close event) is now stale.
    this.run += 1;
    this.stopTimer();
    this.request?.unsubscribe();
    this.request = null;
    const client = this.client;
    this.client = null;
    client?.disconnect();
  }
}
