import { Component, OnInit, inject, output, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Observable, forkJoin, of, catchError } from 'rxjs';
import { ApprovalItem, SchedulerApiService } from '@core/services/scheduler-api.service';
import { ClassesApiService, PendingAddition } from '@core/services/classes-api.service';
import { SchedulerSettingsService } from '@core/services/scheduler-settings.service';
import { at, gb, hhmm, isoDate, localizeUtcWindows } from './schedule.util';

interface Open { id: string; mode: 'propose' | 'reject'; date: string; from: string; to: string; reason: string }

/** A refusal from the approval endpoints: a plain message, or `{message, reasons, next_free}` (409). */
export function approvalError(e: HttpErrorResponse): string {
  const d = e.error?.detail;
  if (typeof d === 'string') return localizeUtcWindows(d);
  if (d && typeof d === 'object' && typeof d.message === 'string') {
    const reasons = Array.isArray(d.reasons) && d.reasons.length ? `: ${d.reasons.map(localizeUtcWindows).join('; ')}` : '';
    const next = d.next_free?.start_time ? `. Next free: ${whenLabel(d.next_free.start_time, d.next_free.end_time)}` : '';
    return `${d.message}${reasons}${next}`;
  }
  return e.status === 0 ? 'The server could not be reached.' : `Request failed (${e.status}).`;
}

export function whenLabel(start: string, end: string): string {
  const s = new Date(start), e = new Date(end);
  const day = s.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  return `${day} ${hhmm(s)}–${hhmm(e)}`;
}

/**
 * The approver's queue on /schedule and in the calendar's Events view side panel
 * (/calendar/events, #285) (ADR 0014 §14-17): class sessions awaiting approval,
 * each with whether it fits and the next free slot, to Approve, Propose a time or Reject;
 * and roll-call additions that did not fit an approved session. Admins and range ops only
 * (schedule:approve).
 */
@Component({
  selector: 'tn-approval-queue',
  standalone: true,
  imports: [FormsModule],
  template: `
  <section class="panel" aria-labelledby="approvals-h">
    <h2 id="approvals-h">Awaiting approval</h2>
    <p class="small muted">Class sessions hold nothing and nobody is invited until approved.</p>
    @for (i of items(); track i.event.id) {
      <div class="item">
        <div><strong>{{ i.event.name }}</strong>@if (i.class_name) { <span class="small muted"> · {{ i.class_name }}</span> }</div>
        <p class="small">{{ when(i.event.start_time, i.event.end_time) }} · {{ i.event.vcpu_total }} vCPU · {{ gb(i.event.ram_mb_total) }} GB</p>
        @if (i.event.approval?.proposed_start_time; as ps) {
          <p class="small">You proposed {{ when(ps, i.event.approval!.proposed_end_time!) }}: waiting for the instructor.</p>
        }
        @if (i.fit.fits) { <span class="chip ok">Fits</span> }
        @else {
          <span class="chip">Doesn’t fit</span>
          <ul class="small reasons">@for (r of i.fit.reasons; track r) { <li>{{ localize(r) }}</li> }</ul>
          <p class="small">Next free: {{ i.fit.next_free ? when(i.fit.next_free.start_time, i.fit.next_free.end_time) : 'none ' + horizon() }}</p>
        }
        @if (open()?.id === i.event.id) {
          @let o = open()!;
          @if (o.mode === 'propose') {
            <div class="three">
              <label><span>Day</span><input class="input" type="date" [ngModel]="o.date" (ngModelChange)="patch({ date: $event })"></label>
              <label><span>From</span><input class="input" type="time" step="900" [ngModel]="o.from" (ngModelChange)="patch({ from: $event })"></label>
              <label><span>To</span><input class="input" type="time" step="900" [ngModel]="o.to" (ngModelChange)="patch({ to: $event })"></label>
            </div>
            <div class="actions"><button class="action primary" [disabled]="busy()" (click)="propose(i)">Send proposal</button>
              <button class="action" (click)="open.set(null)">Back</button></div>
          } @else {
            <label><span>Reason (the instructor sees it)</span>
              <input class="input" [ngModel]="o.reason" (ngModelChange)="patch({ reason: $event })" maxlength="1000"></label>
            <div class="actions"><button class="action primary" [disabled]="busy() || !o.reason.trim()" (click)="reject(i)">Reject</button>
              <button class="action" (click)="open.set(null)">Back</button></div>
          }
        } @else {
          <div class="actions">
            <button class="action primary" [disabled]="busy()" (click)="approve(i)">Approve</button>
            <button class="action" (click)="startPropose(i)">Propose time</button>
            <button class="action" (click)="startReject(i)">Reject</button>
          </div>
        }
      </div>
    } @empty { <p class="small muted">Nothing awaiting approval.</p> }

    @if (additions().length) {
      <h3>Roll-call additions that didn’t fit</h3>
      @for (a of additions(); track a.entry_id) {
        <div class="item">
          <div><strong>{{ a.class_name }}</strong> <span class="small muted">· {{ a.seats }} more seat{{ a.seats === 1 ? '' : 's' }}, {{ a.roll_call_count }} on the roll call</span></div>
          @if (a.reason) { <p class="small">{{ localize(a.reason) }}</p> }
          @if (open()?.id === a.entry_id) {
            <label><span>Reason</span><input class="input" [ngModel]="open()!.reason" (ngModelChange)="patch({ reason: $event })" maxlength="1000"></label>
            <div class="actions"><button class="action primary" [disabled]="busy() || !open()!.reason.trim()" (click)="rejectAddition(a)">Reject</button>
              <button class="action" (click)="open.set(null)">Back</button></div>
          } @else {
            <div class="actions"><button class="action primary" [disabled]="busy()" (click)="approveAddition(a)">Approve</button>
              <button class="action" (click)="open.set({ id: a.entry_id, mode: 'reject', date: '', from: '', to: '', reason: '' })">Reject</button></div>
          }
        </div>
      }
    }
    @if (error(); as err) { <div class="note" role="alert"><strong>Can’t do that</strong><br>{{ err }}</div> }
  </section>
  `,
  styles: [`
    :host { display:block; }
    .panel { background:var(--tn-surface,#fff); border:1px solid var(--tn-line,#eadbdd); border-radius:10px; padding:18px; min-width:0; }
    h2 { font-size:17px; font-weight:600; margin:0 0 6px; } h3 { font-size:14px; font-weight:600; margin:14px 0 6px; }
    p { margin:2px 0 6px; } .small { font-size:12px; } .muted { color:var(--tn-muted,#655b60); }
    .item { padding:11px 0; border-top:1px solid var(--tn-line,#eadbdd); }
    .chip { display:inline-block; padding:3px 7px; border-radius:5px; background:var(--tn-soft,#fcecef); color:var(--tn-accent,#b5122b); font-size:11px; font-weight:500; }
    .chip.ok { background:var(--tn-ok-soft,#e6f3ec); color:var(--tn-ok,#1f6b45); }
    .reasons { margin:4px 0; padding-left:18px; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
    .action { border:1px solid var(--tn-line,#eadbdd); border-radius:7px; padding:6px 11px; background:var(--tn-surface,#fff); cursor:pointer; color:inherit; font:inherit; }
    .action.primary { background:var(--tn-button,#bd1631); color:#fff; border-color:transparent; }
    .action[disabled] { opacity:.45; cursor:not-allowed; }
    label { display:block; margin-top:8px; font-size:13px; } label span { display:block; font-weight:500; margin-bottom:3px; }
    .input { width:100%; box-sizing:border-box; border:1px solid var(--tn-line,#eadbdd); border-radius:7px; padding:6px 8px; font:inherit; color:inherit; background:var(--tn-surface,#fff); }
    .three { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr); gap:8px; }
    .note { padding:10px 12px; border-left:3px solid var(--tn-accent,#b5122b); background:var(--tn-soft,#fcecef); margin-top:10px; font-size:13px; }
    @media (max-width:550px) { .three { grid-template-columns:1fr; } }
  `],
})
export class ApprovalQueueComponent implements OnInit {
  private readonly scheduler = inject(SchedulerApiService);
  private readonly classes = inject(ClassesApiService);
  private readonly settings = inject(SchedulerSettingsService);
  /** How far ahead "next free" was searched: the site's SCHEDULER_NEXT_FREE_DAYS (#318). */
  readonly horizon = this.settings.nextFreeHorizon;

  /** Something was decided: the calendar should reload. */
  readonly changed = output<void>();
  readonly items = signal<ApprovalItem[]>([]);
  readonly additions = signal<PendingAddition[]>([]);
  readonly open = signal<Open | null>(null);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly gb = gb;

  ngOnInit(): void { this.load(); this.settings.load(); }

  load(): void {
    forkJoin({
      q: this.scheduler.approvals().pipe(catchError(() => of({ items: [] }))),
      a: this.classes.pendingAdditions().pipe(catchError(() => of([] as PendingAddition[]))),
    }).subscribe(({ q, a }) => { this.items.set(q.items); this.additions.set(a); });
  }

  when(start: string, end: string): string { return whenLabel(start, end); }
  localize(s: string): string { return localizeUtcWindows(s); }
  patch(p: Partial<Open>): void { const o = this.open(); if (o) this.open.set({ ...o, ...p }); }

  startPropose(i: ApprovalItem): void {
    const slot = i.fit.next_free ?? { start_time: i.event.start_time, end_time: i.event.end_time };
    const s = new Date(slot.start_time), e = new Date(slot.end_time);
    this.error.set(null);
    this.open.set({ id: i.event.id, mode: 'propose', date: isoDate(s), from: hhmm(s), to: hhmm(e), reason: '' });
  }
  startReject(i: ApprovalItem): void {
    this.error.set(null);
    this.open.set({ id: i.event.id, mode: 'reject', date: '', from: '', to: '', reason: '' });
  }

  approve(i: ApprovalItem): void { this.run(this.scheduler.approve(i.event.id)); }
  propose(i: ApprovalItem): void {
    const o = this.open(); if (!o) return;
    this.run(this.scheduler.propose(i.event.id, at(o.date, o.from), at(o.date, o.to)));
  }
  reject(i: ApprovalItem): void {
    const o = this.open(); if (!o) return;
    this.run(this.scheduler.reject(i.event.id, o.reason.trim()));
  }
  approveAddition(a: PendingAddition): void { this.run(this.classes.approveAddition(a.class_id, a.entry_id)); }
  rejectAddition(a: PendingAddition): void {
    const o = this.open(); if (!o) return;
    this.run(this.classes.rejectAddition(a.class_id, a.entry_id, o.reason.trim()));
  }

  private run(call: Observable<unknown>): void {
    this.busy.set(true);
    this.error.set(null);
    call.subscribe({
      next: () => { this.busy.set(false); this.open.set(null); this.load(); this.changed.emit(); },
      error: (e: HttpErrorResponse) => { this.busy.set(false); this.error.set(approvalError(e)); this.load(); },
    });
  }
}
