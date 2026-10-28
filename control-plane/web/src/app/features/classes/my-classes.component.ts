import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { interval } from 'rxjs';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import {
  ClassConsole, ClassesApiService, MyClass, MyClasses, MyRangeConsoles, MyRangeStatus,
} from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { RangeCredentialsComponent } from '../../shared/components/range-credentials/range-credentials.component';
import { CalendarView, addDays, heading, shift, startOfWeek } from '../calendar/content-calendar.util';
import { apiError } from '../ranges/range-capabilities';

/** The window the page asks for: Monday to Monday, or the first of the month to the next. */
export function windowOf(anchor: Date, view: CalendarView): { start: Date; end: Date } {
  if (view === 'week') {
    const start = startOfWeek(anchor);
    return { start, end: addDays(start, 7) };
  }
  return {
    start: new Date(anchor.getFullYear(), anchor.getMonth(), 1),
    end: new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1),
  };
}

/** How often the page asks again while open. */
export const REFRESH_MS = 60_000;

const RANGE_LABEL: Record<MyRangeStatus, string> = {
  not_built: 'Range not built', building: 'Range building', ready: 'Range ready', paused: 'Range paused',
  failed: 'Range failed',
};

export function rangeLabel(status: MyRangeStatus | null | undefined): string {
  return status ? RANGE_LABEL[status] : 'No range';
}

/** Why Join is not live (empty when it is). The API decides; this only explains. */
export function joinHint(c: MyClass): string {
  if (c.join_range_id) return '';
  if (c.seat === 'forfeited') return 'Join opens again once your instructor reinstates you.';
  switch (c.range_status) {
    case 'building': return 'Your range is being built.';
    case 'paused': return 'Your range is powered off. It opens when your instructor starts it.';
    case 'not_built': return 'Your range is built shortly before your session.';
    case 'failed': return 'Your range could not be built. Your instructor can rebuild it.';
    case 'ready': return 'The shared range opens when the session starts.';
    default: return 'No session to come uses a range.';
  }
}

/** The lost-seat message (ADR 0014 §4), as the shell's banner words it. */
export function forfeitMessage(c: MyClass): string {
  if (c.seat !== 'forfeited') return '';
  const by = c.forfeit_deadline
    ? ` by ${new Date(c.forfeit_deadline).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
    : '';
  const who = c.lead_instructor ? `your instructor, ${c.lead_instructor},` : 'your instructor';
  return `You lost your seat: you had not signed in${by}, 2 hours after the class started. Ask ${who} to reinstate you.`;
}

/**
 * My classes (ADR 0014 §20, issue #284): a Student's classes and sessions in the week or the
 * month, their seat, onboarding and range, and Join, which opens their range's accounts in
 * the shared credentials panel. Everything shown is what GET /classes/attending hands the
 * caller, their own entries only; Join's accounts come from /ranges/{id}/credentials, which
 * decides again. Calendar invites by email continue as before.
 */
@Component({
  selector: 'tn-my-classes',
  imports: [
    DatePipe, RouterLink, MatButtonModule, MatButtonToggleModule, MatCardModule, MatIconModule,
    EmptyStateComponent, RangeCredentialsComponent,
  ],
  template: `
    <div class="page-container my-classes">
      <header class="head">
        <div class="titles">
          <div class="eyebrow">My classes</div>
          <h1>{{ title() }}</h1>
        </div>
        <div class="controls">
          <div class="pager">
            <button mat-icon-button [attr.aria-label]="'Previous ' + view()" (click)="move(-1)">
              <mat-icon>chevron_left</mat-icon>
            </button>
            <button mat-button class="today" (click)="today()">Today</button>
            <button mat-icon-button [attr.aria-label]="'Next ' + view()" (click)="move(1)">
              <mat-icon>chevron_right</mat-icon>
            </button>
          </div>
          <mat-button-toggle-group [value]="view()" (change)="setView($event.value)" aria-label="Range"
                                   hideSingleSelectionIndicator>
            <mat-button-toggle value="week">Week</mat-button-toggle>
            <mat-button-toggle value="month">Month</mat-button-toggle>
          </mat-button-toggle-group>
        </div>
      </header>

      @if (loading()) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading your classes"></div>
      }
      @if (loadError()) { <p class="error" role="alert">{{ loadError() }}</p> }

      @if (data(); as d) {
        @for (c of d.classes; track c.id) {
          <mat-card class="class" [attr.data-class]="c.id">
            <mat-card-header>
              <mat-card-title>{{ c.name }}</mat-card-title>
              <mat-card-subtitle>
                {{ c.course_name }}
                @if (c.lead_instructor) { · Lead instructor: {{ c.lead_instructor }} }
                · {{ c.start_date | date:'mediumDate' }} – {{ c.end_date | date:'mediumDate' }}
              </mat-card-subtitle>
            </mat-card-header>
            <mat-card-content>
              <div class="status" role="list" aria-label="Your status">
                <span role="listitem" class="chip" [class.ok]="c.onboarded" [class.warn]="!c.onboarded">
                  {{ c.onboarded ? 'Onboarded' : 'Not onboarded' }}</span>
                <span role="listitem" class="chip" [class.ok]="d.acknowledgement_signed"
                      [class.warn]="!d.acknowledgement_signed">
                  {{ d.acknowledgement_signed ? 'Rules of use signed' : 'Rules of use not signed' }}</span>
                <span role="listitem" class="chip" [class.ok]="c.seat === 'ready'" [class.bad]="c.seat === 'forfeited'">
                  {{ c.seat === 'forfeited' ? 'Lost seat' : 'Seat ready' }}</span>
                @if (c.seat !== 'forfeited') {
                  <span role="listitem" class="chip" [class.ok]="c.range_status === 'ready'"
                        [class.bad]="c.range_status === 'failed'" [class.busy]="c.range_status === 'building' || c.range_status === 'paused'">
                    {{ range(c.range_status) }}</span>
                }
              </div>
              @if (c.seat === 'forfeited') {
                <p class="forfeit" role="alert"><mat-icon>event_busy</mat-icon> {{ forfeit(c) }}</p>
              }
              @if (!d.acknowledgement_signed) {
                <p class="hint"><a routerLink="/rules-of-use">Read and sign the rules of use</a> before your session.</p>
              }

              <ul class="sessions" aria-label="Sessions">
                @for (s of c.sessions; track s.event_id) {
                  <li class="session" [class.next]="s.event_id === c.next_session_id">
                    <span class="when">{{ s.start_time | date:'EEE d MMM, HH:mm' }}–{{ s.end_time | date:'HH:mm' }}</span>
                    <span class="name">{{ s.name }}</span>
                    <span class="mode">{{ s.range_mode === 'individual' ? 'Individual' : 'Collective' }}</span>
                    @if (s.live) { <span class="live">Live now</span> }
                  </li>
                } @empty {
                  <li class="none">No sessions {{ view() === 'week' ? 'this week' : 'this month' }}.</li>
                }
              </ul>
            </mat-card-content>
            <mat-card-actions>
              <button mat-flat-button class="join" [disabled]="!c.join_range_id" (click)="join(c)">
                <mat-icon>login</mat-icon> Join
              </button>
              <button mat-stroked-button class="console" [disabled]="!c.join_range_id" (click)="showConsoles(c)">
                <mat-icon>terminal</mat-icon> Console
              </button>
              @if (!c.join_range_id) { <span class="hint">{{ hint(c) }}</span> }
            </mat-card-actions>
            @if (consoles(); as k) {
              @if (k.classId === c.id && c.join_range_id === k.data.range_id) {
                <section class="my-consoles" [attr.aria-label]="'Consoles: ' + c.name">
                  <h3>Your machines</h3>
                  @if (!k.data.open) {
                    <p class="hint">Your console opens during your session, from shortly before it starts.</p>
                  }
                  <div class="machines">
                    @for (m of k.data.machines; track m.machine) {
                      <button mat-stroked-button [disabled]="!k.data.open || opening()" (click)="openConsole(c, m.machine)">
                        <mat-icon>desktop_windows</mat-icon> {{ m.machine }}
                      </button>
                    } @empty { <p class="hint">No machines of yours have a console on this range.</p> }
                  </div>
                  @if (ticket(); as t) {
                    <p class="ticket">Console ticket for <b>{{ t.machine }}</b> (valid {{ t.expires_in }}s):
                      <code>{{ t.url }}</code></p>
                  }
                  <p class="muted">Opening a console is recorded, and counts for your attendance like your accounts.</p>
                </section>
              }
            }
            @if (joined()?.id === c.id && c.join_range_id) {
              <section class="my-range" [attr.aria-label]="'Your range: ' + c.name">
                <h3>Your range: {{ c.name }}</h3>
                <p class="muted">Open your accounts during each session: that is how your attendance is recorded.</p>
                <tn-range-credentials [rangeId]="c.join_range_id" [autoLoad]="joinKey(c)" />
              </section>
            }
          </mat-card>
        } @empty {
          <tn-empty-state icon="event_available" title="No classes"
            [message]="view() === 'week' ? 'You have no classes this week.' : 'You have no classes this month.'" />
        }
      }
    </div>
  `,
  styles: [`
    .my-classes { display: flex; flex-direction: column; gap: 16px; }
    .head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; }
    .eyebrow { font-size: 13px; color: var(--mat-sys-on-surface-variant); font-weight: 500; }
    h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
    .controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .pager { display: flex; align-items: center; background: var(--mat-sys-surface); border-radius: 12px; padding: 2px; }
    .status { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
    .chip { display: inline-flex; align-items: center; min-height: 28px; padding: 0 12px; border-radius: 14px;
      font-size: 13px; font-weight: 600; border: 1px solid var(--mat-sys-outline-variant);
      color: var(--mat-sys-on-surface-variant); background: var(--mat-sys-surface); }
    .chip.ok { color: var(--mat-sys-on-primary-container); background: var(--mat-sys-primary-container); border-color: transparent; }
    .chip.warn, .chip.busy { color: var(--mat-sys-on-tertiary-container); background: var(--mat-sys-tertiary-container);
      border-color: transparent; }
    .chip.bad { color: var(--mat-sys-on-error-container); background: var(--mat-sys-error-container); border-color: transparent; }
    .forfeit { display: flex; gap: 8px; align-items: center; color: var(--mat-sys-error); margin: 8px 0; }
    .sessions { list-style: none; padding: 0; margin: 8px 0 0; }
    .session { display: flex; flex-wrap: wrap; gap: 6px 12px; padding: 6px 0; border-top: 1px solid var(--mat-sys-outline-variant); }
    .session.next .when { font-weight: 600; }
    .session .mode { color: var(--mat-sys-on-surface-variant); }
    .live { color: var(--mat-sys-primary); font-weight: 600; }
    .none { color: var(--mat-sys-on-surface-variant); padding: 6px 0; }
    .hint, .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; }
    mat-card-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
    .my-range { padding: 0 16px 16px; }
    .my-range h3, .my-consoles h3 { font-size: 15px; margin: 8px 0 4px; }
    .my-consoles { padding: 0 16px 16px; }
    .machines { display: flex; flex-wrap: wrap; gap: 8px; }
    .ticket code { display: block; word-break: break-all; font-size: 0.8rem; margin-top: 4px; }
    .error { color: var(--mat-sys-error); }
  `],
})
export class MyClassesComponent implements OnInit {
  private readonly api = inject(ClassesApiService);
  private readonly notify = inject(NotificationService);

  readonly view = signal<CalendarView>('week');
  readonly anchor = signal(new Date());
  readonly data = signal<MyClasses | null>(null);
  readonly loading = signal(true);
  readonly loadError = signal('');
  /** The class whose Join was pressed (and how many times): its range's accounts are shown
   *  under it, fetched on every press. */
  readonly joined = signal<{ id: string; press: number } | null>(null);
  private presses = 0;
  /** The machines of the class whose Console was pressed, and the last ticket opened. */
  readonly consoles = signal<{ classId: string; data: MyRangeConsoles } | null>(null);
  readonly ticket = signal<ClassConsole | null>(null);
  readonly opening = signal(false);
  readonly title = computed(() => heading(this.anchor(), this.view()));

  readonly range = rangeLabel;
  readonly hint = joinHint;
  readonly forfeit = forfeitMessage;

  constructor() {
    // Ranges come up and sessions start while the page is open: Join follows within a minute.
    interval(REFRESH_MS).pipe(takeUntilDestroyed()).subscribe(() => this.load(true));
  }

  ngOnInit(): void {
    this.load();
  }

  setView(view: CalendarView): void {
    this.view.set(view);
    this.load();
  }
  move(dir: -1 | 1): void {
    this.anchor.set(shift(this.anchor(), this.view(), dir));
    this.load();
  }
  today(): void {
    this.anchor.set(new Date());
    this.load();
  }

  /**
   * Join fetches the accounts at once (GET /ranges/{id}/credentials): that read is the
   * attendance evidence (#283). Every press asks again, and so does a later session while the
   * panel stays open (the key follows the current or next session as the page refreshes), so
   * a Student never sees the morning's accounts without being counted this afternoon.
   */
  join(c: MyClass): void {
    if (c.join_range_id) this.joined.set({ id: c.id, press: ++this.presses });
  }

  /** The Student's machines on their class range (the API decides which, and whether now). */
  showConsoles(c: MyClass): void {
    const rangeId = c.join_range_id;
    if (!rangeId) return;
    this.ticket.set(null);
    this.api.myRangeConsoles(rangeId).subscribe({
      next: data => this.consoles.set({ classId: c.id, data }),
      error: err => this.notify.error(apiError(err)),
    });
  }

  /** A one-time console ticket; the API audits it and counts it as attendance. */
  openConsole(c: MyClass, machine: string): void {
    const rangeId = c.join_range_id;
    if (!rangeId) return;
    this.opening.set(true);
    this.api.openMyRangeConsole(rangeId, machine).subscribe({
      next: t => { this.ticket.set(t); this.opening.set(false); },
      error: err => { this.opening.set(false); this.notify.error(apiError(err)); },
    });
  }

  joinKey(c: MyClass): string | null {
    const j = this.joined();
    return j && j.id === c.id ? `${c.next_session_id ?? 'none'}#${j.press}` : null;
  }

  private load(quiet = false): void {
    const { start, end } = windowOf(this.anchor(), this.view());
    if (!quiet) {
      this.loading.set(true);
      this.loadError.set('');
    }
    this.api.attending(start, end).subscribe({
      next: d => { this.data.set(d); this.loading.set(false); },
      error: err => {
        this.loading.set(false);
        if (quiet) return;  // a background refresh keeps what is shown
        this.loadError.set(apiError(err));
        this.notify.error(apiError(err));
      },
    });
  }
}
