import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { NgTemplateOutlet } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { forkJoin, of, catchError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import {
  Booking, BookingIn, CapacityResult, OvercapacityPolicy, SchedulerApiService, Timeline,
} from '@core/services/scheduler-api.service';
import type { RangeSummary, TemplateSummary, User } from '@core/models';
import {
  DAY_MS, HOLDING_STATES, Level, addDays, at, diskLabel, gb, hhmm, hourOf, isoDate, layoutLanes, level,
  localizeUtcWindows, peakLoad, previewConflicts, sameDay, slotLoads, weekStart,
} from './schedule.util';

interface Form {
  id?: string; name: string; templateId: string; date: string; from: string; to: string;
  rangeId: string; instructorId: string; draft: boolean;
}

const ROW = 46; // px per hour

/**
 * The scheduler (docs/adr/0004-scheduler-module.md, slice 8; design: docs/mockups/scheduler.html).
 * Staff only: Students are kept out by the route guard, the nav and the API.
 */
@Component({
  selector: 'tn-schedule',
  standalone: true,
  imports: [FormsModule, MatSnackBarModule, NgTemplateOutlet],
  template: `
  @if (isRangeOps()) {
    <!-- ── Range Ops: capacity ─────────────────────────────── -->
    <div class="pagehead"><div><span class="eyebrow">Capacity</span><h1>Cluster this week</h1>
      <p class="muted">Committed by bookings, build and teardown time included. Supply: {{ supplyLabel() }}.
        Running ranges with no booking aren't counted yet (ADR 0005).</p></div>
      <div class="weeknav"><button class="action" (click)="shiftWeek(-1)" aria-label="Previous week">←</button><strong>{{ weekLabel() }}</strong><button class="action" (click)="shiftWeek(1)" aria-label="Next week">→</button></div></div>
    <div class="split"><div class="stack">
      <section class="panel"><h2>Load by hour · % of the tightest resource</h2>
        <div class="table-wrap"><table class="heat"><thead><tr><th></th>@for (h of heatHours; track h) {<th>{{ pad(h) }}:00</th>}</tr></thead><tbody>
          @for (d of days(); track d.getTime(); let i = $index) {
            <tr><th>{{ dayLabel(d) }}</th>@for (h of heatHours; track h) {
              @let p = hourLoad(i, h);
              <td><div [class]="'lv-' + lvl(p)" [attr.title]="pct(p) + '%'">{{ p ? pct(p) : '' }}</div></td>}</tr>
          }</tbody></table></div></section>
      <section class="panel"><h2>Bookings</h2><ng-container *ngTemplateOutlet="grid" /></section>
    </div><div class="stack">
      <section class="panel"><h2>Scheduler clock · next builds and teardowns</h2>
        @for (q of clockQueue(); track q.key) {
          <div class="row"><div><strong>{{ q.title }}</strong><p class="small muted">{{ q.when }}{{ q.note ? ' · ' + q.note : '' }}</p></div>
            <span class="chip" [class.ok]="q.kept" [class.neutral]="!q.kept">{{ q.kept ? 'kept' : q.task }}</span></div>
        } @empty { <p class="small muted">Nothing booked to build or tear down this week.</p> }
      </section>
      @if (selected(); as b) { <ng-container *ngTemplateOutlet="detail; context: { $implicit: b }" /> }
      <ng-container *ngTemplateOutlet="feedPanel" />
    </div></div>
  } @else {
    <!-- ── Everyone else on staff: the calendar ─────────────── -->
    <div class="pagehead"><div><span class="eyebrow">Schedule</span><h1>{{ isObserver() ? 'Range schedule' : 'This week' }}</h1>
      <p class="muted">{{ liveCount() }} session{{ liveCount() === 1 ? '' : 's' }} booked{{ busiest() ? ' · busiest ' + busiest() : '' }}. Times are in your time zone ({{ tz }}).</p></div>
      @if (canBook() && !nextSession()) { <button class="action primary" (click)="startBooking()">Book a session</button> }</div>
    @if (nextSession(); as n) {
      <section class="panel hero"><span class="eyebrow">Your next session</span><h2>{{ n.name }}</h2>
        <p>{{ longWhen(n) }}{{ n.range_id ? ' · ' + rangeName(n.range_id) + '. The range builds at ' + buildAt(n) + '; you’ll get a reminder the day before.' : '.' }}</p>
        <div class="actions"><button class="action primary" (click)="startBooking()">Book a session</button></div></section>
    }
    <div class="split"><div>
      <div class="weekbar"><div class="weeknav"><button class="action" (click)="shiftWeek(-1)" aria-label="Previous week">←</button><strong>{{ weekLabel() }}</strong><button class="action" (click)="shiftWeek(1)" aria-label="Next week">→</button></div>
        <div class="legend"><span><i class="lv-ok"></i>under 60%</span><span><i class="lv-mid"></i>60–100%</span><span><i class="lv-high"></i>over</span><span>Edge bar: cluster load, build and teardown time included</span></div></div>
      <ng-container *ngTemplateOutlet="grid" />
    </div><div class="stack">
      @if (form(); as f) {
        <section class="panel"><h2>{{ f.id ? 'Move or resize' : 'Book a session' }}</h2>
          <label class="field"><span>Name</span><input class="input" [ngModel]="f.name" (ngModelChange)="patch({ name: $event })"></label>
          <label class="field"><span>Range template</span>
            <select class="input" [ngModel]="f.templateId" (ngModelChange)="patch({ templateId: $event })">
              <option value="">Choose a template</option>
              @for (t of templates(); track t.id) { <option [value]="t.id">{{ t.name }}</option> }
            </select>
            @if (check(); as c) { <small>{{ c.vm_count_needed }} VMs · {{ c.vcpu_needed }} vCPU · {{ gb(c.ram_mb_needed ?? 0) }} GB RAM · {{ disk(c.disk_gb_needed ?? 0) }} disk, from the template</small> }
          </label>
          <div class="three"><label class="field"><span>Day</span><input class="input" type="date" [ngModel]="f.date" (ngModelChange)="patch({ date: $event })"></label>
            <label class="field"><span>From</span><input class="input" type="time" step="900" [ngModel]="f.from" (ngModelChange)="patch({ from: $event })"></label>
            <label class="field"><span>To</span><input class="input" type="time" step="900" [ngModel]="f.to" (ngModelChange)="patch({ to: $event })"></label></div>
          <div class="two"><label class="field"><span>Range</span>
              <select class="input" [ngModel]="f.rangeId" (ngModelChange)="patch({ rangeId: $event })">
                <option value="">None yet</option>
                @for (r of bookableRanges(); track r.id) { <option [value]="r.id">{{ r.name }}</option> }
              </select></label>
            <div class="field"><label for="sched-instructor">Instructor</label>
              @if (isAdmin()) {
                <select id="sched-instructor" class="input" [ngModel]="f.instructorId" (ngModelChange)="patch({ instructorId: $event })">
                  <option value="">Nobody yet</option>
                  @for (u of instructors(); track u.id) { <option [value]="u.id">{{ u.display_name }}</option> }
                </select>
              } @else { <input id="sched-instructor" class="input" [value]="me()?.display_name ?? ''" disabled> }</div></div>
          @if (check(); as c) {
            <h3>Cluster at that time</h3>
            <ng-container *ngTemplateOutlet="meters; context: { $implicit: c, need: true }" />
            <p class="small muted">Hatched: this booking.{{ c.supply_source === 'env' ? ' Totals are the env fallback until host discovery reports capacity.' : '' }}</p>
          }
          @if (formProblem(); as p) {
            <div class="note"><strong>{{ p.title }}</strong>@for (r of p.reasons; track r) {<br>{{ r }}}
              @if (p.hint) {<br><span class="small muted">{{ p.hint }}</span>}</div>
          } @else if (check()?.fits) {
            <div class="note ok"><strong>Fits.</strong> {{ f.rangeId ? 'The range builds at ' + formBuildAt() + ' and is torn down at ' + formTeardownAt() + '.' : 'No range yet: the capacity is held, and nothing is built until a range is assigned.' }}</div>
          }
          <div class="actions">
            <button class="action primary" [disabled]="!canSubmit() || busy()" (click)="submit(false)">{{ overButWarn() ? 'Book anyway' : (f.id ? 'Save changes' : 'Book it') }}</button>
            @if (!f.id) { <button class="action" [disabled]="!f.name || !f.templateId || busy()" (click)="submit(true)">Save as draft</button> }
            <button class="action" (click)="form.set(null)">Cancel</button>
          </div>
        </section>
      } @else if (selected(); as b) {
        <ng-container *ngTemplateOutlet="detail; context: { $implicit: b }" />
      } @else {
        <section class="panel"><h2>{{ dayLabel(days()[focusDay()]) }} · cluster</h2>
          @if (dayCommitted(); as c) { <ng-container *ngTemplateOutlet="meters; context: { $implicit: c, need: false }" /> }
          <p class="small muted">Peak 09:00–16:00, build and teardown time included.</p></section>
      }
      @if (isAdmin()) {
        <section class="panel"><h2>Over-capacity policy</h2>
          <p class="small muted">For the whole platform: every tenant books against the same cluster. Changes are logged.
            @if (!canChangePolicy()) { Only the platform operator’s administrators can change it. }</p>
          <div class="radio">
            <label [class.on]="policy() === 'block'"><input type="radio" name="pol" [checked]="policy() === 'block'" [disabled]="!canChangePolicy()" (change)="setPolicy('block')"><strong>Block</strong><br><span class="small muted">A booking that doesn’t fit is refused, for everyone.</span></label>
            <label [class.on]="policy() === 'warn'"><input type="radio" name="pol" [checked]="policy() === 'warn'" [disabled]="!canChangePolicy()" (change)="setPolicy('warn')"><strong>Warn</strong><br><span class="small muted">It books, with the reasons shown and logged.</span></label>
          </div></section>
      }
      <ng-container *ngTemplateOutlet="feedPanel" />
    </div></div>
  }

  <!-- ── Week grid ────────────────────────────────────────── -->
  <ng-template #grid>
    @if (narrow()) {
      <div class="daystrip" role="group" aria-label="Day">
        @for (d of days(); track d.getTime(); let i = $index) {
          <button type="button" [attr.aria-pressed]="focusDay() === i" (click)="focusDay.set(i)">
            <strong>{{ d.toLocaleDateString(undefined, { weekday: 'short' }) }}</strong><span>{{ d.getDate() }}</span>
            <i [class]="'lv-' + lvl(dayPeak(i))"></i></button>
        }
      </div>
    }
    <div class="week" [style.grid-template-columns]="'46px repeat(' + gridDays().length + ', minmax(0,1fr))'" role="grid" aria-label="Bookings this week">
      <div class="gutter"></div>
      @for (i of gridDays(); track i) {
        @let d = days()[i];
        @let p = dayPeak(i);
        <button type="button" class="head" [attr.aria-pressed]="focusDay() === i" (click)="focusDay.set(i)">
          <strong>{{ dayLabel(d) }}</strong><span class="small muted">peak {{ pct(p) }}%</span>
          <span class="peak"><i [class]="'lv-' + lvl(p)" [style.width.%]="min100(pct(p))"></i></span></button>
      }
      <div class="times" [style.height.px]="gridHeight()">
        @for (h of hourMarks(); track h; let j = $index) { <span [style.top.px]="j * row">{{ pad(h) }}:00</span> }
      </div>
      @for (i of gridDays(); track i) {
        @let col = columns()[i];
        <div class="day" [style.height.px]="gridHeight()">
          @for (h of hourMarks(); track h; let j = $index) {
            @if (j < hourMarks().length - 1) {
              <div class="hour" [style.top.px]="j * row"></div>
              <div [class]="'load lv-' + lvl(hourLoad(i, h))" [style.top.px]="j * row" [style.height.px]="row" [attr.title]="pct(hourLoad(i, h)) + '% committed'"></div>
            }
          }
          @for (e of col.items; track e.key) {
            @if (e.booking; as b) {
              <button type="button" class="ev" [class.mine]="isMine(b)" [class.draft]="b.state === 'draft'" [class.sel]="selected()?.id === b.id"
                      [style.top.px]="e.top" [style.height.px]="e.height" [style.left]="e.left" [style.width]="e.width" (click)="select(b)">
                <strong>{{ b.name }}</strong><span>{{ hhmm(b.start_time) }}–{{ hhmm(b.end_time) }}{{ b.template_id ? ' · ' + templateName(b.template_id) : '' }}{{ b.state === 'draft' ? ' · draft' : '' }}</span>
                @if (b.instructor_id) { <span class="ghost"><br>{{ personName(b.instructor_id) }}</span> }
              </button>
            } @else {
              <div class="ev proposed" [style.top.px]="e.top" [style.height.px]="e.height" [style.left]="e.left" [style.width]="e.width"><strong>New: {{ form()?.name }}</strong></div>
            }
          }
        </div>
      }
    </div>
  </ng-template>

  <!-- ── Booking detail ───────────────────────────────────── -->
  <ng-template #detail let-b>
    <section class="panel"><span class="eyebrow">{{ dayLabel(toDate(b.start_time)) }}</span><h2 class="title">{{ b.name }}</h2>
      <div class="chips"><span class="chip" [class.ok]="b.state !== 'draft'" [class.neutral]="b.state === 'draft'">{{ stateLabel(b.state) }}</span>
        @if (b.template_id) { <span class="chip neutral">{{ templateName(b.template_id) }}</span> }</div>
      <dl class="kv"><dt>When</dt><dd>{{ hhmm(b.start_time) }}–{{ hhmm(b.end_time) }}</dd>
        <dt>Instructor</dt><dd>{{ b.instructor_id ? personName(b.instructor_id) : 'Nobody yet' }}</dd>
        <dt>Range</dt><dd>{{ b.range_id ? rangeName(b.range_id) : 'none yet' }}</dd>
        <dt>Size</dt><dd>{{ b.vm_count }} VMs · {{ b.vcpu_total }} vCPU · {{ gb(b.ram_mb_total) }} GB · {{ disk(b.disk_gb_total) }}</dd>
        @if (b.range_id && b.state !== 'draft') { <dt>Range builds</dt><dd>{{ buildAt(b) }} · torn down {{ teardownAt(b) }}</dd> }
        @if (b.state !== 'draft' && b.instructor_id) { <dt>Reminder</dt><dd>Emailed to the instructor 24 h before</dd> }</dl>
      @if (actionError(); as err) { <div class="note"><strong>Can’t do that</strong><br>{{ err }}</div> }
      @if (canEdit(b)) {
        <div class="actions">
          @if (b.state === 'draft') { <button class="action primary" [disabled]="busy()" (click)="scheduleDraft(b)">Schedule it</button> }
          @if (b.state === 'draft' || b.state === 'scheduled') { <button class="action" (click)="startEditing(b)">Move or resize</button> }
          @if (isLive(b) || b.state === 'draft') {
            @if (confirmCancel() === b.id) { <button class="action primary" [disabled]="busy()" (click)="cancel(b)">Yes, cancel it</button> }
            @else { <button class="action" (click)="confirmCancel.set(b.id)">Cancel booking</button> }
          }
        </div>
        @if (confirmCancel() === b.id) { <p class="small muted">The range is torn down if the scheduler built it, and the invite is withdrawn.</p> }
      }
    </section>
  </ng-template>

  <!-- ── Capacity meters ──────────────────────────────────── -->
  <ng-template #meters let-c let-need="need">
    @for (m of meterRows(c, need); track m.label) {
      <div class="meter"><span>{{ m.label }}</span>
        <div class="bar"><i [class]="'lv-' + m.lvl" [style.width.%]="m.used"></i>@if (m.add) {<b [style.left.%]="m.used" [style.width.%]="m.add"></b>}</div>
        <span [class.over]="m.over">{{ m.text }}</span></div>
    }
  </ng-template>

  <!-- ── Calendar subscription ────────────────────────────── -->
  <ng-template #feedPanel>
    <section class="panel"><h2>Subscribe in your calendar</h2>
      @if (feedUrl(); as f) {
        <p class="small">Copy it now: it is shown only once. Anyone with it can read this schedule.</p>
        <div class="url">{{ f.url }}</div>
        <div class="actions"><button class="action" (click)="copy(f.url)">Copy link</button><a class="action" [href]="f.webcal_url">Open in calendar app</a><button class="action" (click)="feedUrl.set(null)">Done</button></div>
      } @else {
        <p class="small muted">{{ feedActive() ? 'Your calendar is subscribed. Regenerate the link if it was shared by mistake: the old one stops working.' : 'See this schedule in Outlook, Apple Calendar or Google. Updates appear within a few hours; you also get an emailed invite for your own sessions.' }}</p>
        <div class="actions"><button class="action" [class.primary]="!feedActive()" (click)="issueFeed()">{{ feedActive() ? 'Regenerate link' : 'Get calendar link' }}</button>
          @if (feedActive()) { <button class="action" (click)="revokeFeed()">Turn off</button> }</div>
      }
    </section>
  </ng-template>
  `,
  styles: [`
    :host {
      --tn-bg:#fff7f7; --tn-surface:#ffffff; --tn-ink:#292326; --tn-muted:#655b60; --tn-line:#eadbdd;
      --tn-accent:#b5122b; --tn-soft:#fcecef; --tn-button:#bd1631; --tn-ok:#1f6b45; --tn-ok-soft:#e6f3ec;
      display:block; padding:22px; color:var(--tn-ink); background:var(--tn-bg); min-height:100%;
      font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
    }
    h1 { font-size:27px; line-height:1.18; font-weight:600; letter-spacing:-.8px; margin:5px 0 9px; }
    h2 { font-size:17px; font-weight:600; margin:0 0 13px; } h3 { font-size:14px; font-weight:600; margin:4px 0 5px; }
    p { margin:0 0 12px; } .muted { color:var(--tn-muted); } .small { font-size:12px; }
    .eyebrow { font-size:11px; font-weight:600; letter-spacing:1.2px; text-transform:uppercase; color:var(--tn-accent); }
    .pagehead { display:flex; justify-content:space-between; align-items:flex-end; gap:14px; margin-bottom:18px; flex-wrap:wrap; }
    .pagehead p { max-width:70ch; margin:0; }
    .panel { background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:10px; padding:18px; min-width:0; }
    .hero { background:var(--tn-soft); border:0; margin-bottom:18px; } .hero h2 { font-size:22px; margin:7px 0 9px; }
    .stack { display:grid; gap:16px; align-content:start; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,360px); gap:16px; align-items:start; }
    .actions { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-top:14px; }
    .action { border:1px solid var(--tn-line); border-radius:7px; padding:8px 13px; background:var(--tn-surface); cursor:pointer; color:inherit; text-decoration:none; font:inherit; }
    .action.primary { background:var(--tn-button); color:#fff; border-color:transparent; }
    .action[disabled] { opacity:.45; cursor:not-allowed; }
    .row { padding:11px 0; border-top:1px solid var(--tn-line); display:flex; justify-content:space-between; gap:14px; align-items:center; }
    .row:first-of-type { border-top:0; padding-top:0; } .row p { margin:2px 0 0; }
    .chip { display:inline-block; padding:3px 7px; border-radius:5px; background:var(--tn-soft); color:var(--tn-accent); font-size:11px; font-weight:500; white-space:nowrap; }
    .chip.ok { background:var(--tn-ok-soft); color:var(--tn-ok); } .chip.neutral { background:var(--tn-bg); color:var(--tn-muted); }
    .chips { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; } .title { margin:4px 0 8px; }
    .kv { display:grid; grid-template-columns:110px minmax(0,1fr); gap:6px 12px; font-size:13px; margin:0; } .kv dt { color:var(--tn-muted); } .kv dd { margin:0; }
    .note { padding:12px 14px; border-left:3px solid var(--tn-accent); background:var(--tn-soft); margin:12px 0 0; font-size:13px; }
    .note.ok { border-color:var(--tn-ok); background:var(--tn-ok-soft); }
    .field { display:block; margin-bottom:12px; } .field > span, .field > label { display:block; font-weight:500; margin-bottom:4px; font-size:13px; }
    .field small { display:block; color:var(--tn-muted); margin-top:3px; font-size:12px; }
    .input { width:100%; min-width:0; box-sizing:border-box; background:var(--tn-surface); border:1px solid var(--tn-line); border-radius:7px; padding:7px 9px; font:inherit; color:inherit; }
    .two { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; } .three { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr); gap:10px; }
    .weekbar { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
    .weeknav { display:flex; gap:6px; align-items:center; }
    .legend { display:flex; gap:12px; font-size:12px; color:var(--tn-muted); align-items:center; flex-wrap:wrap; }
    .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
    .daystrip { display:grid; grid-template-columns:repeat(auto-fit, minmax(0,1fr)); gap:5px; margin-bottom:10px; }
    .daystrip button { border:1px solid var(--tn-line); border-radius:8px; padding:6px 4px; background:var(--tn-surface); font:inherit; color:inherit; cursor:pointer; text-align:center; }
    .daystrip button[aria-pressed="true"] { border-color:var(--tn-accent); background:var(--tn-soft); }
    .daystrip strong { display:block; font-size:11px; } .daystrip span { display:block; font-size:16px; font-weight:600; }
    .daystrip i { display:block; height:4px; border-radius:2px; margin-top:4px; }
    .week { display:grid; border:1px solid var(--tn-line); border-radius:10px; overflow:hidden; background:var(--tn-surface); }
    .head { padding:8px 8px 6px; border:0; border-bottom:1px solid var(--tn-line); border-left:1px solid var(--tn-line); font:inherit; font-size:12px; cursor:pointer; background:var(--tn-surface); text-align:left; color:inherit; }
    .head strong { display:block; font-size:13px; } .head[aria-pressed="true"] { background:var(--tn-soft); }
    .head .peak { display:block; margin-top:5px; height:5px; border-radius:3px; background:var(--tn-bg); border:1px solid var(--tn-line); overflow:hidden; }
    .head .peak i { display:block; height:100%; }
    .gutter { border-bottom:1px solid var(--tn-line); }
    .times { position:relative; margin-bottom:8px; } .times span { position:absolute; right:6px; font-size:10px; color:var(--tn-muted); transform:translateY(-6px); }
    .day { position:relative; border-left:1px solid var(--tn-line); }
    .hour { position:absolute; left:0; right:0; border-top:1px dashed var(--tn-line); }
    .load { position:absolute; left:0; width:5px; }
    .ev { position:absolute; border-radius:7px; padding:5px 7px; font:inherit; font-size:11px; line-height:1.3; overflow:hidden; cursor:pointer; border:1px solid var(--tn-line); background:var(--tn-surface); text-align:left; color:inherit; box-sizing:border-box; }
    .ev strong { display:block; font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .ev.mine { background:var(--tn-soft); border-color:var(--tn-accent); } .ev.draft { border-style:dashed; background:var(--tn-bg); color:var(--tn-muted); }
    .ev.sel { box-shadow:0 0 0 3px var(--tn-accent); } .ev .ghost { color:var(--tn-muted); }
    .ev.proposed { border:2px dashed var(--tn-accent); background:rgba(252,236,239,.85); cursor:default; }
    .lv-ok { background:#2f8f5b; } .lv-mid { background:#d99a1e; } .lv-high { background:var(--tn-accent); } .lv-none { background:transparent; }
    .meter { display:grid; grid-template-columns:52px minmax(0,1fr) 110px; gap:8px; align-items:center; font-size:12px; margin:6px 0; }
    .meter .bar { height:8px; border-radius:4px; background:var(--tn-bg); border:1px solid var(--tn-line); overflow:hidden; position:relative; }
    .meter .bar i { position:absolute; top:0; bottom:0; left:0; }
    .meter .bar b { position:absolute; top:0; bottom:0; background:repeating-linear-gradient(135deg,var(--tn-accent) 0 3px,transparent 3px 6px); }
    .meter span:last-child { text-align:right; color:var(--tn-muted); } .meter span.over { color:var(--tn-accent); font-weight:600; }
    .table-wrap { overflow-x:auto; }
    .heat { width:100%; border-collapse:collapse; font-size:12px; } .heat th { font-weight:500; color:var(--tn-muted); text-align:left; padding:5px 6px; white-space:nowrap; }
    .heat td { padding:3px; } .heat td div { height:24px; min-width:30px; border-radius:4px; display:grid; place-items:center; font-size:10px; color:#fff; font-weight:600; }
    .radio { display:flex; gap:8px; flex-wrap:wrap; }
    .radio label { flex:1 1 140px; border:1px solid var(--tn-line); border-radius:8px; padding:10px; cursor:pointer; font-size:13px; }
    .radio label.on { border-color:var(--tn-accent); background:var(--tn-soft); } .radio input { margin-right:6px; }
    .url { font:12px ui-monospace,Menlo,monospace; padding:8px; border-radius:7px; background:var(--tn-bg); border:1px solid var(--tn-line); overflow-wrap:anywhere; }
    @media (max-width:1100px) { .split { grid-template-columns:1fr; } }
    @media (max-width:550px) { :host { padding:14px; } h1 { font-size:23px; } .two, .three { grid-template-columns:1fr; } .ev span { display:none; } }
  `],
})
export class ScheduleComponent implements OnInit, OnDestroy {
  private readonly scheduler = inject(SchedulerApiService);
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly snack = inject(MatSnackBar);

  readonly row = ROW;
  readonly heatHours = Array.from({ length: 10 }, (_, i) => 8 + i);
  readonly tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  readonly gb = gb;
  readonly disk = diskLabel;

  // ── State ──────────────────────────────────────────────
  readonly week = signal(weekStart(new Date()));
  readonly bookings = signal<Booking[]>([]);
  readonly timeline = signal<Timeline | null>(null);
  readonly templates = signal<TemplateSummary[]>([]);
  readonly ranges = signal<RangeSummary[]>([]);
  readonly people = signal<User[]>([]);
  readonly policy = signal<OvercapacityPolicy>('block');
  readonly canChangePolicy = signal(false);
  readonly selected = signal<Booking | null>(null);
  readonly focusDay = signal(0);
  readonly form = signal<Form | null>(null);
  readonly check = signal<CapacityResult | null>(null);
  readonly submitError = signal<string | null>(null);
  readonly actionError = signal<string | null>(null);
  readonly confirmCancel = signal<string | null>(null);
  readonly busy = signal(false);
  readonly feedActive = signal(false);
  readonly feedUrl = signal<{ url: string; webcal_url: string } | null>(null);
  private checkTimer: ReturnType<typeof setTimeout> | undefined;
  /** Phones: one day at a time, picked from a strip. */
  private readonly narrowQuery = window.matchMedia('(max-width: 600px)');
  readonly narrow = signal(this.narrowQuery.matches);
  private readonly onNarrow = (e: MediaQueryListEvent) => this.narrow.set(e.matches);
  readonly gridDays = computed(() => (this.narrow() ? [Math.min(this.focusDay(), this.days().length - 1)] : this.days().map((_, i) => i)));

  // ── Who is looking ─────────────────────────────────────
  readonly me = this.auth.user;
  readonly role = computed(() => this.me()?.role ?? null);
  readonly isAdmin = computed(() => this.role() === 'admin');
  readonly isRangeOps = computed(() => this.role() === 'range_ops');
  readonly isObserver = computed(() => this.role() === 'observer');
  readonly canBook = computed(() => this.role() === 'admin' || this.role() === 'instructor');

  // ── Derived ────────────────────────────────────────────
  readonly lead = computed(() => this.timeline()?.lead_minutes ?? 30);
  readonly grace = computed(() => this.timeline()?.grace_minutes ?? 15);
  readonly loads = computed(() => (this.timeline() ? slotLoads(this.timeline()!) : []));
  readonly visible = computed(() => this.bookings().filter(b => b.state !== 'cancelled'));
  readonly days = computed(() => {
    const weekend = this.visible().some(b => [0, 6].includes(new Date(b.start_time).getDay()));
    return Array.from({ length: weekend ? 7 : 5 }, (_, i) => addDays(this.week(), i));
  });
  readonly hourRange = computed(() => {
    let lo = 7, hi = 18;
    for (const b of this.visible()) {
      const s = new Date(b.start_time), e = new Date(b.end_time);
      lo = Math.min(lo, s.getHours()); hi = Math.max(hi, e.getHours() + (e.getMinutes() ? 1 : 0));
    }
    return { lo, hi: Math.min(24, Math.max(hi, lo + 1)) };
  });
  readonly hourMarks = computed(() => {
    const { lo, hi } = this.hourRange();
    return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  });
  readonly gridHeight = computed(() => (this.hourRange().hi - this.hourRange().lo) * ROW);
  readonly columns = computed(() => {
    const { lo, hi } = this.hourRange();
    const f = this.form();
    const proposal = f && f.date && f.from && f.to ? { start: at(f.date, f.from), end: at(f.date, f.to) } : null;
    return this.days().map(day => {
      const spans = this.visible()
        .filter(b => sameDay(new Date(b.start_time), day) && b.id !== f?.id)
        .map(b => ({ booking: b as Booking | null, key: b.id, start: hourOf(new Date(b.start_time), day), end: hourOf(new Date(b.end_time), day) }));
      if (proposal && sameDay(proposal.start, day)) spans.push({ booking: null, key: 'proposal', start: hourOf(proposal.start, day), end: hourOf(proposal.end, day) });
      const items = layoutLanes(spans).map(({ item, lane, lanes }) => {
        const s = Math.max(item.start, lo), e = Math.min(item.end, hi);
        return {
          ...item,
          top: (s - lo) * ROW, height: Math.max(18, (e - s) * ROW),
          left: `calc(9px + (100% - 13px) * ${lane / lanes})`, width: `calc((100% - 13px) / ${lanes} - 3px)`,
        };
      });
      return { day, items };
    });
  });
  readonly liveCount = computed(() => this.visible().filter(b => HOLDING_STATES.includes(b.state)).length);
  readonly busiest = computed(() => {
    const peaks = this.days().map((_, i) => this.dayPeak(i));
    const top = Math.max(...peaks, 0);
    return top > 0 ? this.dayLabel(this.days()[peaks.indexOf(top)]) : '';
  });
  readonly nextSession = computed(() => {
    const me = this.me()?.id;
    if (this.role() !== 'instructor' || !me) return null;
    const now = Date.now();
    return this.visible()
      .filter(b => b.instructor_id === me && HOLDING_STATES.includes(b.state) && new Date(b.end_time).getTime() > now)
      .sort((a, b) => a.start_time.localeCompare(b.start_time))[0] ?? null;
  });
  readonly instructors = computed(() => this.people().filter(u => u.role === 'instructor' || u.role === 'admin'));
  readonly bookableRanges = computed(() => this.ranges().filter(r => r.state !== 'destroyed' && r.state !== 'destroying'));
  readonly dayCommitted = computed(() => {
    const t = this.timeline(); if (!t) return null;
    const day = this.days()[this.focusDay()]; if (!day) return null;
    const from = new Date(day.getTime() + 9 * 3_600_000), to = new Date(day.getTime() + 16 * 3_600_000);
    let worst = t.buckets[0] ? { v: 0, r: 0, d: 0 } : null;
    for (const b of t.buckets) {
      const at_ = new Date(b.time);
      if (at_ >= from && at_ < to && worst) {
        worst = { v: Math.max(worst.v, b.vcpu_committed), r: Math.max(worst.r, b.ram_mb_committed), d: Math.max(worst.d, b.disk_gb_committed) };
      }
    }
    if (!worst) return null;
    return { vcpu_committed: worst.v, ram_mb_committed: worst.r, disk_gb_committed: worst.d, vcpu_total: t.cluster_vcpu, ram_mb_total: t.cluster_ram_mb, disk_gb_total: t.cluster_disk_gb } as unknown as CapacityResult;
  });
  readonly formConflicts = computed(() => {
    const f = this.form(); if (!f || !f.date || !f.from || !f.to) return [];
    return previewConflicts(
      { id: f.id, start: at(f.date, f.from), end: at(f.date, f.to), rangeId: f.rangeId || null, instructorId: this.formInstructor(f) },
      this.bookings(), this.lead(), this.grace(),
    );
  });
  readonly formProblem = computed(() => {
    const f = this.form(); if (!f) return null;
    if (this.submitError()) return { title: 'Not booked', reasons: [localizeUtcWindows(this.submitError()!)], hint: '' };
    if (f.date && f.from && f.to && at(f.date, f.to) <= at(f.date, f.from)) return { title: 'Check the times', reasons: ['The end must be after the start.'], hint: '' };
    const clashes = this.formConflicts();
    if (clashes.length) {
      return { title: 'Can’t book: double-booked', hint: '', reasons: clashes.map(b =>
        `${b.range_id && b.range_id === f.rangeId ? 'Range ' + this.rangeName(b.range_id) : this.personName(b.instructor_id ?? '')} is already booked for '${b.name}' ${this.hhmm(b.start_time)}–${this.hhmm(b.end_time)}`) };
    }
    const c = this.check();
    if (c && !c.fits) {
      return this.policy() === 'block'
        ? { title: 'Doesn’t fit the cluster', reasons: (c.reasons ?? [c.message]).map(localizeUtcWindows), hint: 'The over-capacity policy is Block. Move it, pick a smaller range, or ask an administrator.' }
        : { title: 'Over capacity: will book with a warning', reasons: (c.reasons ?? [c.message]).map(localizeUtcWindows), hint: 'The policy is Warn: it books, and the warning is logged.' };
    }
    return null;
  });
  readonly overButWarn = computed(() => !!this.check() && !this.check()!.fits && this.policy() === 'warn' && !this.formConflicts().length);
  readonly canSubmit = computed(() => {
    const f = this.form(); if (!f || !f.name || !f.templateId || !f.date || !f.from || !f.to) return false;
    if (at(f.date, f.to) <= at(f.date, f.from) || this.formConflicts().length) return false;
    const c = this.check();
    return !!c && (c.fits || this.policy() === 'warn');
  });
  readonly clockQueue = computed(() => {
    const now = Date.now(), lead = this.lead() * 60_000, grace = this.grace() * 60_000;
    const live = this.visible().filter(b => b.range_id && HOLDING_STATES.includes(b.state));
    const out: { key: string; at: number; title: string; when: string; note: string; task: string; kept: boolean }[] = [];
    for (const b of live) {
      const build = new Date(b.start_time).getTime() - lead, down = new Date(b.end_time).getTime() + grace;
      const heir = live.find(o => o.id !== b.id && o.range_id === b.range_id && o.start_time > b.start_time);
      if (build > now) out.push({ key: b.id + ':b', at: build, title: `Build ${this.rangeName(b.range_id!)}`, when: this.shortWhen(build), note: `for '${b.name}' · ${b.vm_count} VMs`, task: 'provision_range', kept: false });
      if (down > now) out.push({ key: b.id + ':d', at: down, title: `Tear down ${this.rangeName(b.range_id!)}`, when: this.shortWhen(down), note: heir ? `kept up: inherited by '${heir.name}'` : '', task: 'destroy_range', kept: !!heir });
    }
    return out.sort((a, b) => a.at - b.at).slice(0, 12);
  });

  // ── Lifecycle ──────────────────────────────────────────
  ngOnInit(): void {
    this.narrowQuery.addEventListener('change', this.onNarrow);
    const today = this.days().findIndex(d => sameDay(d, new Date()));
    if (today >= 0) this.focusDay.set(today);
    this.load();
    this.api.listTemplates(200).subscribe({ next: t => this.templates.set(t), error: () => {} });
    this.api.listRanges(200).subscribe({ next: r => this.ranges.set(r), error: () => {} });
    if (this.canBook()) this.api.listUsers().subscribe({ next: u => this.people.set(u), error: () => {} });
    this.scheduler.getPolicy().subscribe({
      next: p => { this.policy.set(p.overcapacity); this.canChangePolicy.set(!!p.can_change); },
      error: () => {},
    });
    this.scheduler.feedStatus().subscribe({ next: f => this.feedActive.set(f.active), error: () => {} });
  }

  ngOnDestroy(): void {
    clearTimeout(this.checkTimer);
    this.narrowQuery.removeEventListener('change', this.onNarrow);
  }

  load(): void {
    const from = this.week(), to = new Date(from.getTime() + 7 * DAY_MS);
    forkJoin({
      list: this.scheduler.list(from, to).pipe(catchError(() => of({ items: [], total: 0 }))),
      tl: this.scheduler.timeline(from, 7, 15).pipe(catchError(() => of(null))),
    }).subscribe(({ list, tl }) => {
      this.bookings.set(list.items);
      this.timeline.set(tl);
      const sel = this.selected();
      if (sel) this.selected.set(list.items.find(b => b.id === sel.id) ?? null);
    });
  }

  shiftWeek(n: number): void {
    this.week.set(addDays(this.week(), 7 * n));
    this.selected.set(null);
    this.focusDay.set(0);
    this.load();
  }

  // ── Selection and booking ──────────────────────────────
  select(b: Booking): void {
    this.form.set(null);
    this.actionError.set(null);
    this.confirmCancel.set(null);
    this.selected.set(this.selected()?.id === b.id ? null : b);
    const i = this.days().findIndex(d => sameDay(d, new Date(b.start_time)));
    if (i >= 0) this.focusDay.set(i);
  }

  startBooking(): void {
    const focused = this.days()[this.focusDay()] ?? this.week();
    const today = new Date(new Date().setHours(0, 0, 0, 0));
    const day = focused < today ? today : focused;
    const me = this.me();
    this.selected.set(null);
    this.submitError.set(null);
    this.check.set(null);
    this.form.set({
      name: '', templateId: '', date: isoDate(day), from: '09:00', to: '12:00', rangeId: '',
      instructorId: this.role() === 'instructor' && me ? me.id : '', draft: false,
    });
  }

  startEditing(b: Booking): void {
    const s = new Date(b.start_time), e = new Date(b.end_time);
    this.submitError.set(null);
    this.form.set({
      id: b.id, name: b.name, templateId: b.template_id ?? '', date: isoDate(s), from: hhmm(s), to: hhmm(e),
      rangeId: b.range_id ?? '', instructorId: b.instructor_id ?? '', draft: b.state === 'draft',
    });
    this.selected.set(null);
    this.runCheck();
  }

  patch(p: Partial<Form>): void {
    const f = this.form(); if (!f) return;
    this.form.set({ ...f, ...p });
    this.submitError.set(null);
    clearTimeout(this.checkTimer);
    this.checkTimer = setTimeout(() => this.runCheck(), 300);
  }

  private runCheck(): void {
    const f = this.form();
    if (!f || !f.templateId || !f.date || !f.from || !f.to || at(f.date, f.to) <= at(f.date, f.from)) {
      this.check.set(null);
      return;
    }
    this.scheduler.check({
      start_time: at(f.date, f.from).toISOString(), end_time: at(f.date, f.to).toISOString(), template_id: f.templateId,
    }).subscribe({ next: c => this.check.set(c), error: () => this.check.set(null) });
  }

  submit(draft: boolean): void {
    const f = this.form(); if (!f) return;
    const body: BookingIn = {
      name: f.name, start_time: at(f.date, f.from).toISOString(), end_time: at(f.date, f.to).toISOString(),
      template_id: f.templateId || null, range_id: f.rangeId || null, instructor_id: this.formInstructor(f), draft,
    };
    this.busy.set(true);
    const call = f.id ? this.scheduler.update(f.id, body) : this.scheduler.create(body);
    call.subscribe({
      next: b => {
        this.busy.set(false);
        this.form.set(null);
        this.check.set(null);
        const warn = b.warnings?.length ? ` Over capacity: ${b.warnings.join('; ')}` : '';
        this.snack.open(`${draft ? 'Draft saved' : f.id ? 'Booking updated' : 'Booked'}: ${b.name}.${warn}`, 'OK', { duration: warn ? 8000 : 3000 });
        this.load();
      },
      error: (e: HttpErrorResponse) => { this.busy.set(false); this.submitError.set(this.detail(e)); },
    });
  }

  scheduleDraft(b: Booking): void {
    this.act(this.scheduler.schedule(b.id), `Scheduled: ${b.name}`);
  }

  cancel(b: Booking): void {
    this.act(this.scheduler.cancel(b.id), `Cancelled: ${b.name}`, () => this.selected.set(null));
  }

  private act(call: ReturnType<SchedulerApiService['cancel']>, done: string, after?: () => void): void {
    this.busy.set(true);
    this.actionError.set(null);
    call.subscribe({
      next: () => { this.busy.set(false); this.confirmCancel.set(null); this.snack.open(done, '', { duration: 3000 }); after?.(); this.load(); },
      error: (e: HttpErrorResponse) => { this.busy.set(false); this.actionError.set(this.detail(e)); },
    });
  }

  setPolicy(p: OvercapacityPolicy): void {
    this.scheduler.setPolicy(p).subscribe({
      next: r => { this.policy.set(r.overcapacity); this.snack.open(`Over-capacity policy: ${r.overcapacity}`, '', { duration: 2500 }); this.runCheck(); },
      error: () => this.snack.open('Could not change the policy', 'OK', { duration: 5000 }),
    });
  }

  // ── Calendar feed ──────────────────────────────────────
  issueFeed(): void {
    this.scheduler.issueFeed().subscribe({
      next: f => { this.feedUrl.set(f); this.feedActive.set(true); },
      error: () => this.snack.open('Could not create a calendar link', 'OK', { duration: 5000 }),
    });
  }

  revokeFeed(): void {
    this.scheduler.revokeFeed().subscribe({
      next: () => { this.feedUrl.set(null); this.feedActive.set(false); this.snack.open('Calendar link turned off', '', { duration: 2000 }); },
      error: () => this.snack.open('Could not turn off the calendar link', 'OK', { duration: 5000 }),
    });
  }

  copy(url: string): void {
    navigator.clipboard?.writeText(url).then(() => this.snack.open('Link copied', '', { duration: 2000 }), () => {});
  }

  // ── View helpers ───────────────────────────────────────
  canEdit(b: Booking): boolean {
    const me = this.me()?.id;
    return this.isAdmin() || (this.role() === 'instructor' && !!me && (b.instructor_id === me || b.created_by === me));
  }
  isMine(b: Booking): boolean { return !!this.me()?.id && b.instructor_id === this.me()!.id; }
  isLive(b: Booking): boolean { return HOLDING_STATES.includes(b.state); }
  hourLoad(dayIndex: number, h: number): number {
    const d = this.days()[dayIndex]; if (!d) return 0;
    const from = new Date(d.getFullYear(), d.getMonth(), d.getDate(), h);
    return peakLoad(this.loads(), from, new Date(from.getTime() + 3_600_000));
  }
  dayPeak(dayIndex: number): number {
    const d = this.days()[dayIndex]; if (!d) return 0;
    const { lo, hi } = this.hourRange();
    return peakLoad(this.loads(), new Date(d.getFullYear(), d.getMonth(), d.getDate(), lo), new Date(d.getFullYear(), d.getMonth(), d.getDate(), hi));
  }
  meterRows(c: CapacityResult, need: boolean) {
    const row = (label: string, used: number, add: number, total: number, fmt: (n: number) => string) => {
      const u = total ? Math.min(100, (used / total) * 100) : 0;
      const a = total ? Math.min(100 - u, (add / total) * 100) : 0;
      return { label, used: u, add: a, lvl: level(total ? used / total : 0), over: used + add > total, text: `${fmt(used + add)} / ${fmt(total)}` };
    };
    return [
      row('vCPU', c.vcpu_committed - (need && c.fits ? (c.vcpu_needed ?? 0) : 0), need ? (c.vcpu_needed ?? 0) : 0, c.vcpu_total, n => String(n)),
      row('RAM', c.ram_mb_committed - (need && c.fits ? (c.ram_mb_needed ?? 0) : 0), need ? (c.ram_mb_needed ?? 0) : 0, c.ram_mb_total, n => `${gb(n)} GB`),
      row('Disk', c.disk_gb_committed - (need && c.fits ? (c.disk_gb_needed ?? 0) : 0), need ? (c.disk_gb_needed ?? 0) : 0, c.disk_gb_total, diskLabel),
    ];
  }
  supplyLabel(): string {
    const t = this.timeline();
    if (!t) return 'not loaded';
    const src = t.supply_source === 'env' ? 'env fallback' : t.supply_source;
    return `${src} (${t.cluster_vcpu} vCPU · ${gb(t.cluster_ram_mb)} GB · ${diskLabel(t.cluster_disk_gb)})`;
  }
  rangeName(id: string): string { return this.ranges().find(r => r.id === id)?.name ?? 'a range'; }
  templateName(id: string): string { return this.templates().find(t => t.id === id)?.name ?? 'template'; }
  personName(id: string): string {
    if (id && id === this.me()?.id) return this.me()!.display_name;
    return this.people().find(u => u.id === id)?.display_name ?? 'Instructor';
  }
  stateLabel(s: string): string { return s.charAt(0).toUpperCase() + s.slice(1); }
  buildAt(b: Booking): string { return hhmm(new Date(new Date(b.start_time).getTime() - this.lead() * 60_000)); }
  teardownAt(b: Booking): string { return hhmm(new Date(new Date(b.end_time).getTime() + this.grace() * 60_000)); }
  formBuildAt(): string { const f = this.form()!; return hhmm(new Date(at(f.date, f.from).getTime() - this.lead() * 60_000)); }
  formTeardownAt(): string { const f = this.form()!; return hhmm(new Date(at(f.date, f.to).getTime() + this.grace() * 60_000)); }
  dayLabel(d: Date): string { return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }); }
  weekLabel(): string {
    const a = this.week(), b = this.days()[this.days().length - 1];
    return `${a.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} – ${b.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}`;
  }
  longWhen(b: Booking): string { return `${this.dayLabel(new Date(b.start_time))}, ${this.hhmm(b.start_time)}–${this.hhmm(b.end_time)}`; }
  shortWhen(ms: number): string { const d = new Date(ms); return `${this.dayLabel(d)} ${hhmm(d)}`; }
  hhmm(iso: string): string { return hhmm(new Date(iso)); }
  toDate(iso: string): Date { return new Date(iso); }
  pad(h: number): string { return String(h).padStart(2, '0'); }
  pct(f: number): number { return Math.round(f * 100); }
  min100(n: number): number { return Math.min(100, n); }
  lvl(f: number): Level { return level(f); }

  private formInstructor(f: Form): string | null {
    if (f.instructorId) return f.instructorId;
    return this.role() === 'instructor' ? (this.me()?.id ?? null) : null;
  }

  private detail(e: HttpErrorResponse): string {
    const d = e.error?.detail;
    return typeof d === 'string' ? localizeUtcWindows(d) : e.status === 0 ? 'The server could not be reached.' : `Request failed (${e.status}).`;
  }
}
