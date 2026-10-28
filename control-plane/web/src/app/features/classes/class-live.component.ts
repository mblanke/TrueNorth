import { Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { interval } from 'rxjs';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { ClassConsole, ClassLive, ClassesApiService, LiveRange } from '@core/services/classes-api.service';
import type { components } from '@core/api/schema';

type LiveMachine = components['schemas']['ConsoleMachineOut'];
import { NotificationService } from '@core/services/notification.service';
import { apiError } from '../ranges/range-capabilities';

/** How often the live view asks again while open. */
export const LIVE_REFRESH_MS = 30_000;

/** What an observer is told about their consoles (ADR 0014 §19). */
export function observedNote(live: ClassLive): string {
  if (live.role !== 'observer') return '';
  return live.view_only_consoles
    ? 'Your consoles are view-only: the hypervisor ignores your keyboard and mouse.'
    : 'Your consoles are marked observed: this hypervisor cannot make them view-only, so do not type '
      + 'into a Student\'s machine. Every console you open is recorded.';
}

/**
 * A class session's live view (ADR 0014 §19, issue #321): each Student on the roll call with
 * their seat's machines (an individual session) or their team and the shared range (a
 * collective one), when they last opened it, and a console on any machine during the
 * session. For the class's lead, assistants, observers and admins; the API decides.
 */
@Component({
  selector: 'tn-class-live',
  imports: [DatePipe, RouterLink, MatButtonModule, MatCardModule, MatIconModule],
  template: `
    <div class="page-container class-live">
      <a mat-button routerLink="/classes" [queryParams]="{ class: classId() }"><mat-icon>arrow_back</mat-icon> Class</a>
      @if (problem()) { <p class="error" role="alert">{{ problem() }}</p> }
      @if (live(); as l) {
        <header>
          <div class="eyebrow">Live · {{ l.class_name }}</div>
          <h1>{{ l.session_name }}</h1>
          <p class="muted">
            {{ l.start_time | date:'EEE d MMM, HH:mm' }}–{{ l.end_time | date:'HH:mm' }} ·
            {{ l.range_mode === 'individual' ? 'Individual ranges' : 'Collective (shared range)' }}
            @if (l.live) { · <span class="now">Live now</span> }
          </p>
          @if (note()) { <p class="observed" role="note"><mat-icon>visibility</mat-icon> {{ note() }}</p> }
          @if (!l.consoles_open) { <p class="muted">Consoles open during the session, from shortly before it starts.</p> }
        </header>

        @if (l.shared_range; as r) {
          <mat-card class="shared">
            <mat-card-header><mat-card-title>Shared range: {{ r.name }}</mat-card-title>
              <mat-card-subtitle>{{ r.state }}</mat-card-subtitle></mat-card-header>
            <mat-card-content>
              <div class="machines">
                @for (m of r.machines; track m.name) {
                  <button mat-stroked-button [disabled]="!can(l, m) || busy()" (click)="open(r, m.name)">
                    <mat-icon>desktop_windows</mat-icon> {{ m.name }}
                  </button>
                }
              </div>
            </mat-card-content>
          </mat-card>
        }

        @if (ticket(); as t) {
          <p class="ticket" role="status">
            Console ticket for <b>{{ t.vm }}</b> (valid {{ t.expires_in }}s)
            @if (t.observed) { <span class="tag">observed</span> }
            @if (t.view_only) { <span class="tag">view only</span> }:
            <code>{{ t.url }}</code>
          </p>
        }

        <table class="grid" aria-label="Students">
          <thead><tr><th>Student</th>@if (l.range_mode === 'collective') {<th>Team</th>}<th>Range</th>
            <th>Machines</th><th>Last opened</th></tr></thead>
          <tbody>
            @for (s of l.students; track s.entry_id) {
              <tr [attr.data-entry]="s.entry_id">
                <td>{{ s.display_name }}</td>
                @if (l.range_mode === 'collective') { <td>{{ s.team?.name ?? '—' }}</td> }
                <td>{{ s.range ? s.range.state : (l.shared_range ? 'shared' : 'not built') }}</td>
                <td class="machines">
                  @if (s.range; as r) {
                    @for (m of r.machines; track m.name) {
                      <button mat-stroked-button [disabled]="!can(l, m) || busy()" (click)="open(r, m.name)"
                              [attr.aria-label]="'Console on ' + m.name">
                        {{ m.machine }}
                      </button>
                    }
                  }
                </td>
                <td>{{ s.last_seen_at ? (s.last_seen_at | date:'HH:mm') : 'Not yet' }}</td>
              </tr>
            } @empty { <tr><td colspan="5">Nobody is on the roll call.</td></tr> }
          </tbody>
        </table>
      } @else if (!problem()) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading the live view"></div>
      }
    </div>
  `,
  styles: [`
    .class-live { display: flex; flex-direction: column; gap: 16px; }
    .eyebrow { font-size: 13px; color: var(--mat-sys-on-surface-variant); font-weight: 500; }
    h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
    .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; }
    .now { color: var(--mat-sys-primary); font-weight: 600; }
    .observed { display: flex; gap: 8px; align-items: center; padding: 8px 12px; border-radius: 8px;
      color: var(--mat-sys-on-tertiary-container); background: var(--mat-sys-tertiary-container); }
    .machines { display: flex; flex-wrap: wrap; gap: 6px; }
    .grid { width: 100%; border-collapse: collapse; }
    .grid th, .grid td { text-align: left; padding: 6px 8px; border-top: 1px solid var(--mat-sys-outline-variant); }
    .ticket code { display: block; word-break: break-all; font-size: 0.8rem; margin-top: 4px; }
    .tag { margin-left: 6px; padding: 0 8px; border-radius: 10px; font-size: 12px;
      background: var(--mat-sys-tertiary-container); color: var(--mat-sys-on-tertiary-container); }
    .error { color: var(--mat-sys-error); }
  `],
})
export class ClassLiveComponent implements OnInit {
  private readonly api = inject(ClassesApiService);
  private readonly notify = inject(NotificationService);

  /** Route parameters (withComponentInputBinding). */
  readonly classId = input.required<string>();
  readonly eventId = input.required<string>();

  readonly live = signal<ClassLive | null>(null);
  readonly ticket = signal<ClassConsole | null>(null);
  readonly problem = signal('');
  readonly busy = signal(false);
  readonly note = computed(() => {
    const l = this.live();
    return l ? observedNote(l) : '';
  });

  constructor() {
    interval(LIVE_REFRESH_MS).pipe(takeUntilDestroyed()).subscribe(() => this.load(true));
  }

  ngOnInit(): void {
    this.load();
  }

  /** Whether a console can be opened on it now: observers watch the Students' machines only. */
  can(l: ClassLive, m: LiveMachine): boolean {
    return l.consoles_open && (l.role !== 'observer' || !!m.student_machine);
  }

  /** A one-time console on one VM; the API audits it as observer or staff. */
  open(range: LiveRange, vm: string): void {
    this.busy.set(true);
    this.api.openSessionConsole(this.classId(), this.eventId(), range.range_id, vm).subscribe({
      next: t => { this.ticket.set(t); this.busy.set(false); },
      error: err => { this.busy.set(false); this.notify.error(apiError(err)); },
    });
  }

  private load(quiet = false): void {
    this.api.sessionLive(this.classId(), this.eventId()).subscribe({
      next: l => { this.live.set(l); this.problem.set(''); },
      error: err => { if (!quiet) this.problem.set(apiError(err)); },
    });
  }
}
