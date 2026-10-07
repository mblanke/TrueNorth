import { Component, OnInit, inject, signal } from '@angular/core';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MySession, SchedulerApiService } from '@core/services/scheduler-api.service';
import { hhmm } from './schedule.util';

/**
 * A Student's own sessions and their calendar link (ADR 0004 slice 10). Students never
 * see the schedule itself: only the sessions of courses they are enrolled in.
 */
@Component({
  selector: 'tn-my-sessions',
  standalone: true,
  imports: [MatSnackBarModule],
  template: `
    <section class="panel" aria-labelledby="my-sessions-title">
      <h2 id="my-sessions-title">Your sessions</h2>
      @for (s of sessions(); track s.id) {
        <div class="row"><div><strong>{{ s.name }}</strong><p class="small muted">{{ when(s) }}</p></div></div>
      } @empty {
        <p class="small muted">Nothing booked for your courses yet.</p>
      }
      @if (feedUrl(); as f) {
        <p class="small">Copy it now: it is shown only once.</p>
        <div class="url">{{ f.url }}</div>
        <div class="actions"><button class="action" (click)="copy(f.url)">Copy link</button>
          <a class="action" [href]="f.webcal_url">Open in calendar app</a>
          <button class="action" (click)="feedUrl.set(null)">Done</button></div>
      } @else {
        <div class="actions">
          <button class="action" [class.primary]="!feedActive()" (click)="issue()">{{ feedActive() ? 'Regenerate calendar link' : 'Add to my calendar' }}</button>
          @if (feedActive()) { <button class="action" (click)="revoke()">Turn off</button> }
        </div>
        <p class="small muted">Your sessions appear in Outlook, Apple Calendar or Google, and you get an invite by email.</p>
      }
    </section>
  `,
  styles: [`
    :host { --tn-line:#eadbdd; --tn-soft:#fcecef; --tn-accent:#b5122b; --tn-muted:#655b60; display:block; margin:16px 0; }
    .panel { background:#fff; border:1px solid var(--tn-line); border-radius:10px; padding:18px; }
    h2 { font-size:17px; font-weight:600; margin:0 0 12px; }
    .row { padding:9px 0; border-top:1px solid var(--tn-line); } .row:first-of-type { border-top:0; padding-top:0; }
    .row p { margin:2px 0 0; } .small { font-size:12px; } .muted { color:var(--tn-muted); }
    .actions { display:flex; gap:10px; flex-wrap:wrap; margin:12px 0 8px; }
    .action { border:1px solid var(--tn-line); border-radius:7px; padding:8px 13px; background:#fff; cursor:pointer; color:inherit; text-decoration:none; font:inherit; }
    .action.primary { background:#bd1631; color:#fff; border-color:transparent; }
    .url { font:12px ui-monospace,Menlo,monospace; padding:8px; border-radius:7px; background:#fff7f7; border:1px solid var(--tn-line); overflow-wrap:anywhere; }
  `],
})
export class MySessionsComponent implements OnInit {
  private readonly scheduler = inject(SchedulerApiService);
  private readonly snack = inject(MatSnackBar);

  readonly sessions = signal<MySession[]>([]);
  readonly feedActive = signal(false);
  readonly feedUrl = signal<{ url: string; webcal_url: string } | null>(null);

  ngOnInit(): void {
    this.scheduler.mine().subscribe({ next: s => this.sessions.set(s), error: () => {} });
    this.scheduler.feedStatus().subscribe({ next: f => this.feedActive.set(f.active), error: () => {} });
  }

  when(s: MySession): string {
    const a = new Date(s.start_time), b = new Date(s.end_time);
    return `${a.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}, ${hhmm(a)}–${hhmm(b)}`;
  }

  issue(): void {
    this.scheduler.issueFeed().subscribe({
      next: f => { this.feedUrl.set(f); this.feedActive.set(true); },
      error: () => this.snack.open('Could not create a calendar link', 'OK', { duration: 5000 }),
    });
  }

  revoke(): void {
    this.scheduler.revokeFeed().subscribe({
      next: () => { this.feedUrl.set(null); this.feedActive.set(false); },
      error: () => this.snack.open('Could not turn off the calendar link', 'OK', { duration: 5000 }),
    });
  }

  copy(url: string): void {
    navigator.clipboard?.writeText(url).then(() => this.snack.open('Link copied', '', { duration: 2000 }), () => {});
  }
}
