import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatIconModule } from '@angular/material/icon';
import { Observable, catchError, forkJoin, of } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { Booking, BookingIn, SchedulerApiService, Timeline } from '@core/services/scheduler-api.service';
import { MaintenanceWindow, MaintenanceWindowsApiService } from '@core/services/maintenance-windows-api.service';
import { ClassRecord, ClassesApiService } from '@core/services/classes-api.service';
import { NotificationService } from '@core/services/notification.service';
import { PAGE_ACCESS, roleCan, roleHasAccess } from '@core/auth/permissions';
import type { User } from '@core/models';
import { ApprovalQueueComponent, approvalError, whenLabel } from '../schedule/approval-queue.component';
import { HOLDING_STATES, gb, hhmm, isoDate } from '../schedule/schedule.util';
import { CalendarView, addDays, dayKey, gridDays, heading, shift } from './content-calendar.util';
import {
  ALL_ON, Band, Block, EventKind, Filters, HOUR_PX, KIND_STYLE, blockClasses, chipsFor, dayBlocks, dayLoad, hourWindow,
  inMaintenance, isPending, kindOf, laneStyle, legendCounts, loadBar, loadLevel, loadText, maintenanceBands,
  movedTimes, visibleBookings, windowsInPeriod,
} from './events-calendar.util';

interface WeekDay { key: string; date: Date; label: string; today: boolean; blocks: Block[]; bands: Band[];
  used: number; extra: number; level: string; loadText: string }
interface MonthDay { key: string; date: Date; label: string; inPeriod: boolean; today: boolean; chips: Booking[];
  maintenance: MaintenanceWindow[]; used: number; extra: number; level: string; loadText: string }
/** Move: a new start; the booking keeps its length (multi-day and overnight ones move whole). */
interface Move { date: string; from: string }

/** Chips a month cell shows before "+N more". */
const MONTH_CHIPS = 3;
/** The most the list endpoints return in one call. */
const LIST_LIMIT = 200;

/**
 * The calendar's Events view (ADR 0014 decision 21, issue #285): classes in blue (a range
 * per Student) and exercises in orange (one shared range) on a week's timed grid or a
 * month of chips; dashed when awaiting approval, grey stripes for maintenance windows;
 * each day's load and the extra if pending bookings were approved. The side panel holds
 * the legend filters, the approval queue (approvers only: admins and range ops) and the
 * selected booking. Booking a new session stays on /schedule, the booking workbench.
 * Staff with schedule:read only; observers read; Students have My classes instead.
 */
@Component({
  selector: 'tn-events-calendar',
  imports: [FormsModule, RouterLink, MatButtonModule, MatButtonToggleModule, MatIconModule, ApprovalQueueComponent],
  template: `
    <div class="page-container events-cal">
      <header class="cal-header">
        <div class="titles">
          <div class="eyebrow">Classes and exercises</div>
          <h1>{{ title() }}</h1>
        </div>
        <div class="controls">
          <nav class="segmented" aria-label="Calendar views">
            <span class="on" aria-current="page">Events</span>
            @if (canSeeContent()) { <a routerLink="/calendar/content">Content</a> }
          </nav>
          <div class="pager">
            <button mat-icon-button [attr.aria-label]="'Previous ' + view()" (click)="move(-1)">
              <mat-icon>chevron_left</mat-icon>
            </button>
            <button mat-button (click)="today()">Today</button>
            <button mat-icon-button [attr.aria-label]="'Next ' + view()" (click)="move(1)">
              <mat-icon>chevron_right</mat-icon>
            </button>
          </div>
          <mat-button-toggle-group [value]="view()" (change)="setView($event.value)" aria-label="Range"
                                   hideSingleSelectionIndicator>
            <mat-button-toggle value="week">Week</mat-button-toggle>
            <mat-button-toggle value="month">Month</mat-button-toggle>
          </mat-button-toggle-group>
          @if (canBook()) { <a mat-flat-button routerLink="/schedule">Book a session</a> }
        </div>
      </header>

      @if (loadError()) { <p class="error" role="alert">{{ loadError() }}</p> }
      @if (truncated(); as t) {
        <p class="warn" role="status">Showing {{ t.shown }} of {{ t.total }} bookings in this period: switch to Week to see the rest.</p>
      }

      <div class="layout">
        <section class="grid-card" [attr.aria-label]="view() === 'week' ? 'Bookings this week' : 'Bookings this month'">
          @if (view() === 'week') {
            <div class="week" [style.--hour.px]="hourPx">
              <div class="corner"></div>
              @for (d of week(); track d.key) {
                <div class="head" [class.today]="d.today">
                  <div class="dname"><span>{{ d.label }}</span></div>
                  @if (timelineError()) {
                    <div class="captext">Load unavailable</div>
                  } @else {
                    <div class="cap" role="img" [attr.aria-label]="'Load ' + d.loadText" [title]="'Load ' + d.loadText">
                      <i [class]="'used ' + d.level" [style.width.%]="d.used"></i>
                      @if (d.extra) { <i class="extra" [style.left.%]="d.used" [style.width.%]="d.extra"></i> }
                    </div>
                    <div class="captext" aria-hidden="true">{{ d.loadText }}</div>
                  }
                </div>
              }
              <div class="times" [style.height.px]="gridHeight()">
                @for (h of hours(); track h; let j = $index) { <span [style.top.px]="j * hourPx">{{ pad(h) }}:00</span> }
              </div>
              @for (d of week(); track d.key) {
                <div class="day" [style.height.px]="gridHeight()">
                  @for (h of hours(); track h; let j = $index) { <div class="hour" [style.top.px]="j * hourPx"></div> }
                  @for (m of d.bands; track m.window.id) {
                    <div class="maint" [style.top.px]="m.top" [style.height.px]="m.height"
                         [title]="'Maintenance: ' + m.window.reason"><span>Maintenance</span></div>
                  }
                  @for (e of d.blocks; track e.booking.id) {
                    @let b = e.booking;
                    @let pos = lane(e);
                    <button type="button" [class]="classes(b)" [class.selected]="b.id === selectedId()"
                            [attr.aria-pressed]="b.id === selectedId()"
                            [style.--c]="colour(b)" [style.top.px]="e.top" [style.height.px]="e.height"
                            [style.left]="pos.left" [style.width]="pos.width" (click)="select(b)"
                            [attr.aria-label]="ariaLabel(b)">
                      <span class="ev-title"><mat-icon class="kind-icon" aria-hidden="true">{{ icon(b) }}</mat-icon>{{ b.name }}</span>
                      <span class="ev-time">{{ time(b) }}</span>
                    </button>
                  }
                </div>
              }
            </div>
          } @else {
            <div class="month">
              @for (d of weekdays; track d) { <div class="dow">{{ d }}</div> }
              @for (d of month(); track d.key) {
                <div class="cell" [class.out]="!d.inPeriod" [class.maint]="d.maintenance.length > 0"
                     [title]="d.maintenance.length ? 'Maintenance: ' + d.maintenance[0].reason : ''">
                  <div class="cell-head">
                    <span class="num" [class.today]="d.today">{{ d.label }}</span>
                    @if (!timelineError() && (d.used || d.extra)) {
                      <span class="mini-cap" aria-hidden="true">
                        <i [class]="'used ' + d.level" [style.width.%]="d.used"></i>
                        @if (d.extra) { <i class="extra" [style.left.%]="d.used" [style.width.%]="d.extra"></i> }
                      </span>
                    }
                    <span class="sr-only">{{ timelineError() ? 'Load unavailable' : 'Load ' + d.loadText }}@for (m of d.maintenance; track m.id) {<span>. Maintenance: {{ m.reason }}</span>}</span>
                  </div>
                  @for (b of d.chips.slice(0, monthChips); track b.id) {
                    <button type="button" [class]="'chip ' + classes(b)" [class.selected]="b.id === selectedId()"
                            [attr.aria-pressed]="b.id === selectedId()"
                            [style.--c]="colour(b)" (click)="select(b)" [attr.aria-label]="ariaLabel(b)">
                      <mat-icon class="kind-icon" aria-hidden="true">{{ icon(b) }}</mat-icon>
                      <span class="t">{{ hhmmOf(b.start_time) }} {{ b.name }}</span>
                    </button>
                  }
                  @if (d.chips.length > monthChips) {
                    <button type="button" class="more" (click)="openDay(d.date)">+{{ d.chips.length - monthChips }} more</button>
                  }
                </div>
              }
            </div>
          }
        </section>

        <aside class="side">
          <section class="card" aria-label="Legend and filters">
            <div class="card-head"><h2>Show</h2></div>
            <div class="legend" role="group" aria-label="Filters">
              @for (k of kinds; track k) {
                <button type="button" class="filter" [class.off]="!filters()[k]" [attr.aria-pressed]="filters()[k]"
                        [style.--c]="style[k].colour" (click)="toggle(k)" [title]="style[k].hint">
                  <mat-icon class="kind-icon" aria-hidden="true">{{ style[k].icon }}</mat-icon>
                  {{ style[k].label }}<span class="count">{{ counts()[k] }}</span>
                </button>
              }
              <button type="button" class="filter pending-swatch" [class.off]="!filters().pending"
                      [attr.aria-pressed]="filters().pending" (click)="toggle('pending')">
                <span class="swatch dashed"></span>Awaiting approval<span class="count">{{ counts().pending }}</span>
              </button>
              <button type="button" class="filter" [class.off]="!filters().maintenance"
                      [attr.aria-pressed]="filters().maintenance" (click)="toggle('maintenance')">
                <span class="swatch striped"></span>Maintenance<span class="count">{{ counts().maintenance }}</span>
              </button>
            </div>
            <p class="muted">Day bars: cluster load, build and teardown included; the lighter part is what pending bookings would add.</p>
          </section>

          @if (isApprover()) {
            <div class="queue"><tn-approval-queue (changed)="load()" /></div>
          }

          <section class="card" aria-label="Selected booking">
            @if (selected(); as b) {
              <div class="tags">
                <span class="tag" [style.--c]="colour(b)">
                  <mat-icon class="kind-icon" aria-hidden="true">{{ icon(b) }}</mat-icon>{{ kindLabel(b) }}
                </span>
                <span class="kind" [class.pending-tag]="pending(b)">{{ stateLabel(b.state) }}</span>
              </div>
              <h2 class="sel-title">{{ b.name }}</h2>
              <dl class="facts">
                <dt>When</dt><dd>{{ when(b) }}</dd>
                @if (b.class_id) { <dt>Class</dt><dd>{{ className(b) }}</dd> }
                <dt>Ranges</dt><dd>{{ style[kind(b)].hint }}</dd>
                <dt>Instructor</dt><dd>{{ b.instructor_id ? personName(b.instructor_id) : 'Nobody yet' }}</dd>
                <dt>Size</dt><dd>{{ b.vm_count }} VMs · {{ b.vcpu_total }} vCPU · {{ gbOf(b.ram_mb_total) }} GB</dd>
              </dl>
              @if (pending(b)) {
                <p class="note">Awaiting approval by an admin or range ops. Nothing is held and nobody is invited until then.</p>
                @if (b.approval?.proposed_start_time; as ps) {
                  <p class="note">Proposed instead: <strong>{{ whenOf(ps, b.approval!.proposed_end_time!) }}</strong></p>
                  @if (canAnswer(b)) {
                    <div class="actions">
                      @if (b.can_accept) { <button mat-flat-button [disabled]="busy()" (click)="acceptProposal(b)">Accept</button> }
                      @if (b.can_decline) { <button mat-stroked-button [disabled]="busy()" (click)="declineProposal(b)">Decline</button> }
                    </div>
                  }
                }
              }
              @if (b.state === 'rejected') {
                <p class="note">Rejected{{ b.approval?.reason ? ': ' + b.approval!.reason : '' }}</p>
              }
              <div class="links">
                @if (b.class_id && canSeeClasses()) {
                  <a mat-stroked-button [routerLink]="['/classes']" [queryParams]="{ class: b.class_id }">
                    <mat-icon>checklist</mat-icon>Roll call</a>
                }
                @if (canSeeLive() && (b.exercise_id || b.class_id)) {
                  <a mat-stroked-button [routerLink]="b.exercise_id ? ['/ops-center', b.exercise_id] : ['/ops-center/select']">
                    <mat-icon>cast_for_education</mat-icon>Live Class</a>
                }
                @if (canMove(b) && !moving()) {
                  <button mat-stroked-button (click)="startMove(b)"><mat-icon>schedule</mat-icon>Move</button>
                }
              </div>
              @if (moving(); as m) {
                <div class="move" role="group" aria-label="Move booking">
                  <label><span>Day</span><input type="date" [ngModel]="m.date" (ngModelChange)="patchMove({ date: $event })"></label>
                  <label><span>Starts</span><input type="time" step="900" [ngModel]="m.from" (ngModelChange)="patchMove({ from: $event })"></label>
                </div>
                @if (movedEnd(b); as end) { <p class="muted move-end">Ends {{ end }}, the same length as now.</p> }
                @if (b.class_id && b.state === 'scheduled') {
                  <p class="muted">Moving an approved class session sends it back for approval.</p>
                }
                <div class="actions">
                  <button mat-flat-button [disabled]="busy() || !moveValid()" (click)="saveMove(b)">Save</button>
                  <button mat-button (click)="moving.set(null)">Cancel</button>
                </div>
              }
              @if (actionError()) { <p class="error" role="alert">{{ actionError() }}</p> }
            } @else {
              <p class="muted">Select a booking to see its details.</p>
            }
          </section>
        </aside>
      </div>
    </div>
  `,
  styles: [`
    .events-cal { display: flex; flex-direction: column; gap: 20px;
      --tn-surface: var(--mat-sys-surface); --tn-line: var(--mat-sys-outline-variant);
      --tn-muted: var(--mat-sys-on-surface-variant); --tn-button: var(--mat-sys-primary);
      --tn-accent: var(--mat-sys-error); --tn-soft: var(--mat-sys-error-container); }
    .cal-header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; }
    .eyebrow { font-size: 13px; color: var(--mat-sys-on-surface-variant); font-weight: 500; }
    h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }
    .controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .segmented { display: flex; background: var(--mat-sys-surface-container-high); border-radius: 12px; padding: 4px; gap: 2px; }
    .segmented a, .segmented span { padding: 0 18px; height: 36px; border-radius: 9px; display: flex; align-items: center;
      font-size: 14px; font-weight: 500; color: var(--mat-sys-on-surface-variant); text-decoration: none; }
    .segmented .on { background: var(--mat-sys-surface); color: var(--mat-sys-on-surface); font-weight: 600;
      box-shadow: 0 1px 2px rgba(17, 24, 39, 0.08); }
    .pager { display: flex; align-items: center; background: var(--mat-sys-surface); border-radius: 12px; padding: 2px; }
    .layout { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start; }
    .grid-card { flex: 999 1 760px; min-width: 0; overflow-x: auto; background: var(--mat-sys-surface);
      border-radius: 18px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }

    /* week */
    .week { min-width: 900px; padding: 8px; display: grid; grid-template-columns: 52px repeat(7, minmax(0, 1fr)); column-gap: 4px; }
    .head { padding: 8px 6px 10px; display: flex; flex-direction: column; gap: 6px; }
    .dname { font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--mat-sys-outline); }
    .head.today .dname span { background: var(--mat-sys-primary); color: var(--mat-sys-on-primary); border-radius: 12px; padding: 2px 8px; }
    .cap, .mini-cap { position: relative; height: 6px; border-radius: 3px; background: var(--mat-sys-surface-container-high); overflow: hidden; }
    .cap i, .mini-cap i { position: absolute; top: 0; bottom: 0; left: 0; }
    .used.ok { background: #17B26A; } .used.mid { background: #F79009; } .used.high { background: #F04438; }
    .extra { background: repeating-linear-gradient(135deg, #F79009 0 3px, color-mix(in srgb, #F79009 35%, transparent) 3px 6px); }
    .captext { font-size: 11px; color: var(--mat-sys-on-surface-variant); }
    .times { position: relative; }
    .times span { position: absolute; right: 6px; transform: translateY(-50%); font-size: 11px; color: var(--mat-sys-outline); }
    .times span:first-child { transform: none; }
    .day { position: relative; border-radius: 10px; background: var(--mat-sys-surface-container-lowest, var(--mat-sys-surface));
      border: 1px solid var(--mat-sys-outline-variant); overflow: hidden; }
    .hour { position: absolute; left: 0; right: 0; border-top: 1px solid var(--mat-sys-outline-variant); opacity: 0.6; }
    .hour:first-child { border-top: 0; }
    .maint { position: absolute; left: 0; right: 0; z-index: 0; display: flex; align-items: flex-start; padding: 4px 6px;
      box-sizing: border-box; font-size: 10px; font-weight: 600; color: #475467;
      background: repeating-linear-gradient(135deg, #EAECF0 0 6px, #F9FAFB 6px 12px); }
    .ev { position: absolute; z-index: 1; box-sizing: border-box; padding: 4px 6px; margin: 0; overflow: hidden; text-align: left;
      border: 1.5px solid transparent; border-radius: 8px; cursor: pointer; font: inherit; display: flex; flex-direction: column; gap: 1px;
      background: color-mix(in srgb, var(--c) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c) 70%, var(--mat-sys-on-surface));
      border-left: 4px solid var(--c); }
    .week .ev { margin-right: 2px; width: calc(100% - 2px); }
    .ev-title { display: flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 600; line-height: 1.25; }
    .ev-time { font-size: 11px; opacity: 0.85; }
    .kind-icon { font-size: 15px; width: 15px; height: 15px; flex: none; }
    .ev.pending { border: 1.5px dashed var(--c); background: color-mix(in srgb, var(--c) 6%, var(--mat-sys-surface)); }
    .ev.draft { opacity: 0.65; border-style: dotted; }
    .ev.rejected { opacity: 0.5; text-decoration: line-through; }
    .ev.done { opacity: 0.7; }
    .ev.selected { box-shadow: 0 0 0 2px var(--mat-sys-primary); }

    /* month */
    .month { min-width: 900px; padding: 8px; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
    .dow { padding: 12px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-outline); }
    .cell { min-height: 112px; box-sizing: border-box; padding: 8px; border-radius: 12px; display: flex; flex-direction: column; gap: 4px;
      border: 1px solid var(--mat-sys-outline-variant); background: var(--mat-sys-surface); min-width: 0; }
    .cell.out { background: var(--mat-sys-surface-container-low); }
    .cell.out .num { color: var(--mat-sys-outline); }
    .cell.maint { background: repeating-linear-gradient(135deg, #EAECF0 0 6px, #F9FAFB 6px 12px); }
    .cell-head { display: flex; align-items: center; gap: 8px; }
    .mini-cap { flex: 1; height: 4px; }
    .num { font-size: 12px; font-weight: 600; }
    .num.today { display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 24px;
      padding: 0 6px; box-sizing: border-box; border-radius: 12px; background: var(--mat-sys-primary); color: var(--mat-sys-on-primary); }
    .chip.ev { position: static; flex-direction: row; align-items: center; gap: 4px; width: 100%; padding: 3px 6px;
      font-size: 11px; font-weight: 600; line-height: 1.3; }
    .chip .t { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .more { border: 0; background: none; font: inherit; font-size: 11px; font-weight: 600; text-align: left; padding: 2px 6px;
      color: var(--mat-sys-primary); cursor: pointer; }

    /* side */
    .side { flex: 1 1 320px; min-width: 280px; display: flex; flex-direction: column; gap: 16px; }
    .card { background: var(--mat-sys-surface); border-radius: 18px; padding: 18px; display: flex; flex-direction: column;
      gap: 10px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .queue ::ng-deep .panel { border: 0; border-radius: 18px;
      box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .card-head h2 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-on-surface-variant); }
    .legend { display: flex; flex-wrap: wrap; gap: 8px; }
    .filter { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 0 12px; border-radius: 18px; border: 0;
      font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
      background: color-mix(in srgb, var(--c, #667085) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c, #667085) 70%, var(--mat-sys-on-surface)); }
    .filter.off { background: var(--mat-sys-surface); color: var(--mat-sys-on-surface-variant); border: 1px solid var(--mat-sys-outline-variant); }
    .filter .count { font-size: 12px; font-weight: 500; opacity: 0.7; }
    .swatch { width: 14px; height: 10px; border-radius: 3px; flex: none; box-sizing: border-box; }
    .swatch.dashed { border: 1.5px dashed #667085; }
    .swatch.striped { background: repeating-linear-gradient(135deg, #98A2B3 0 2px, #F2F4F7 2px 4px); }
    .tags { display: flex; gap: 8px; flex-wrap: wrap; }
    .tag { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 8px; font-size: 12px; font-weight: 600;
      background: color-mix(in srgb, var(--c) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c) 70%, var(--mat-sys-on-surface)); }
    .kind { padding: 3px 10px; border-radius: 8px; font-size: 12px; font-weight: 600;
      background: var(--mat-sys-surface-container-high); color: var(--mat-sys-on-surface-variant); }
    .kind.pending-tag { background: #FFFAEB; color: #B54708; }
    .sel-title { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
    .facts { display: grid; grid-template-columns: 92px 1fr; row-gap: 8px; column-gap: 12px; font-size: 13px; margin: 0; }
    .facts dt { color: var(--mat-sys-on-surface-variant); }
    .facts dd { margin: 0; }
    .note { margin: 0; font-size: 13px; border-radius: 10px; padding: 8px 12px; background: #FFFAEB; color: #93370D; }
    .links, .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .move { display: grid; grid-template-columns: 1.3fr 1fr; gap: 8px; }
    .move label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .move input { font: inherit; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface); color: var(--mat-sys-on-surface); min-width: 0; }
    .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; margin: 0; }
    .warn { margin: 0; font-size: 13px; border-radius: 10px; padding: 8px 12px; background: #FFFAEB; color: #93370D; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    .cell-head { position: relative; }
    .error { color: var(--mat-sys-error); font-size: 13px; margin: 0; }
  `],
})
export class EventsCalendarComponent implements OnInit {
  private readonly scheduler = inject(SchedulerApiService);
  private readonly maintenance = inject(MaintenanceWindowsApiService);
  private readonly classesApi = inject(ClassesApiService);
  private readonly core = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly auth = inject(AuthService);

  readonly weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  readonly kinds: EventKind[] = ['class', 'exercise'];
  readonly style = KIND_STYLE;
  readonly hourPx = HOUR_PX;
  readonly monthChips = MONTH_CHIPS;

  readonly view = signal<CalendarView>('week');
  readonly anchor = signal(new Date());
  readonly bookings = signal<Booking[]>([]);
  readonly timeline = signal<Timeline | null>(null);
  /** The timeline could not be read: say so rather than show 0%. */
  readonly timelineError = signal(false);
  /** The period has more bookings than the list returned. */
  readonly truncated = signal<{ shown: number; total: number } | null>(null);
  /** Only the latest load may land. */
  private loadSeq = 0;
  readonly windows = signal<MaintenanceWindow[]>([]);
  readonly classList = signal<ClassRecord[]>([]);
  readonly people = signal<User[]>([]);
  readonly filters = signal<Filters>({ ...ALL_ON });
  readonly selectedId = signal<string | null>(null);
  readonly moving = signal<Move | null>(null);
  readonly busy = signal(false);
  readonly loadError = signal('');
  readonly actionError = signal('');

  // ── Who is looking (presentation only: the API refuses the rest) ──
  readonly role = computed(() => this.auth.user()?.role ?? null);
  /** schedule:approve: admins and range ops decide class bookings (ADR 0014 §14). */
  readonly isApprover = computed(() => roleCan(this.role(), 'schedule:approve'));
  readonly canBook = computed(() => roleCan(this.role(), 'schedule:write'));
  readonly canSeeContent = computed(() => roleHasAccess(this.role(), PAGE_ACCESS.contentCalendar));
  readonly canSeeClasses = computed(() => roleHasAccess(this.role(), PAGE_ACCESS.classes));
  readonly canSeeLive = computed(() => roleHasAccess(this.role(), PAGE_ACCESS.opsCenter));

  readonly title = computed(() => heading(this.anchor(), this.view()));
  readonly shown = computed(() => visibleBookings(this.bookings(), this.filters()));
  /** The windows overlapping the period on screen. */
  private readonly periodWindows = computed(() => {
    const days = this.days();
    return windowsInPeriod(this.windows(), days[0], addDays(days[days.length - 1], 1));
  });
  readonly shownWindows = computed(() => (this.filters().maintenance ? this.periodWindows() : []));
  readonly counts = computed(() => legendCounts(this.bookings(), this.periodWindows()));
  readonly selected = computed(() => this.bookings().find(b => b.id === this.selectedId()) ?? null);

  private readonly days = computed(() => gridDays(this.anchor(), this.view()));
  private readonly span = computed(() =>
    hourWindow(this.shown(), this.view() === 'week' ? this.days() : [], this.shownWindows()));
  readonly hours = computed(() => {
    const { lo, hi } = this.span();
    return Array.from({ length: hi - lo }, (_, i) => lo + i);
  });
  readonly gridHeight = computed(() => (this.span().hi - this.span().lo) * HOUR_PX);

  readonly week = computed<WeekDay[]>(() => {
    const { lo, hi } = this.span();
    const todayKey = dayKey(new Date());
    return this.days().map(d => {
      const l = dayLoad(this.timeline(), this.bookings(), d);
      const bar = loadBar(l);
      return {
        level: loadLevel(l),
        key: dayKey(d), date: d, today: dayKey(d) === todayKey,
        label: `${this.weekdays[(d.getDay() + 6) % 7]} ${d.getDate()}`,
        blocks: dayBlocks(this.shown(), d, lo, hi),
        bands: maintenanceBands(this.shownWindows(), d, lo, hi),
        used: bar.used, extra: bar.extra, loadText: loadText(l),
      };
    });
  });

  readonly month = computed<MonthDay[]>(() => {
    const anchor = this.anchor();
    const todayKey = dayKey(new Date());
    return this.days().map(d => {
      const l = dayLoad(this.timeline(), this.bookings(), d);
      const bar = loadBar(l);
      return {
        level: loadLevel(l), loadText: loadText(l),
        key: dayKey(d), date: d, today: dayKey(d) === todayKey,
        label: d.getDate() === 1 ? `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })}` : String(d.getDate()),
        inPeriod: d.getMonth() === anchor.getMonth(),
        chips: chipsFor(this.shown(), d),
        maintenance: inMaintenance(this.shownWindows(), d),
        used: bar.used, extra: bar.extra,
      };
    });
  });

  ngOnInit(): void {
    this.load();
    if (this.canSeeClasses()) {
      const classes = this.auth.isAdmin() ? this.classesApi.list() : this.classesApi.mine();
      classes.subscribe({ next: c => this.classList.set(c ?? []), error: () => {} });
    }
    if (roleCan(this.role(), 'user:read')) {
      this.core.listUsers().subscribe({ next: u => this.people.set(u), error: () => {} });
    }
  }

  // ── navigation ──
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
  /** "+N more" in a month cell: that day's week. */
  openDay(d: Date): void {
    this.anchor.set(d);
    this.setView('week');
  }
  toggle(k: keyof Filters): void {
    this.filters.set({ ...this.filters(), [k]: !this.filters()[k] });
  }

  /**
   * The period's bookings (the API filters by the window), its capacity timeline and the
   * maintenance windows. Past windows are asked for only when the whole period is past: the
   * list is soonest first and capped, so asking for history would crowd out upcoming ones.
   * A load that a later one has overtaken is dropped.
   */
  load(): void {
    const seq = ++this.loadSeq;
    const days = this.days();
    const from = days[0], to = addDays(days[days.length - 1], 1);
    const week = this.view() === 'week';
    forkJoin({
      list: this.scheduler.list(from, to, LIST_LIMIT).pipe(catchError((e: HttpErrorResponse) => of(approvalError(e)))),
      tl: this.scheduler.timeline(from, days.length, week ? 15 : 60).pipe(catchError(() => of(null))),
      mw: this.maintenance.list(to.getTime() <= Date.now(), LIST_LIMIT).pipe(catchError(() => of(null))),
    }).subscribe(({ list, tl, mw }) => {
      if (seq !== this.loadSeq) return;
      if (typeof list === 'string') {
        this.loadError.set(list);
      } else {
        this.bookings.set(list.items);
        this.loadError.set('');
        this.truncated.set(list.total > list.items.length ? { shown: list.items.length, total: list.total } : null);
      }
      this.timeline.set(tl);
      this.timelineError.set(tl === null);
      if (mw) this.windows.set(mw.items);
    });
  }

  // ── selection ──
  select(b: Booking): void {
    this.selectedId.set(this.selectedId() === b.id ? null : b.id);
    this.moving.set(null);
    this.actionError.set('');
  }

  /** The server's own rule, per booking (`can_*` on EventOut, scheduler/change_rule.py, #319):
   *  a class session by its lead or an admin. The page never reimplements it. */
  canMove(b: Booking): boolean { return !!b.can_move; }
  canAnswer(b: Booking): boolean { return !!b.can_accept || !!b.can_decline; }

  startMove(b: Booking): void {
    const s = new Date(b.start_time);
    this.actionError.set('');
    this.moving.set({ date: isoDate(s), from: hhmm(s) });
  }
  patchMove(p: Partial<Move>): void {
    const m = this.moving();
    if (m) this.moving.set({ ...m, ...p });
  }
  moveValid(): boolean {
    const m = this.moving();
    return !!m && !!m.date && !!m.from && !isNaN(movedTimes({ start_time: '', end_time: '' }, m.date, m.from).start.getTime());
  }
  /** Where the moved booking ends: "Thu 7 Mar 17:00". */
  movedEnd(b: Booking): string {
    const m = this.moving();
    if (!m || !this.moveValid()) return '';
    const end = movedTimes(b, m.date, m.from).end;
    return `${end.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} ${hhmm(end)}`;
  }

  /** Same booking, new times: everything else is sent as it is so the move changes nothing else. */
  saveMove(b: Booking): void {
    const m = this.moving();
    if (!m || !this.moveValid()) return;
    const { start, end } = movedTimes(b, m.date, m.from);
    const body: BookingIn = {
      name: b.name, description: b.description, start_time: start.toISOString(), end_time: end.toISOString(),
      // The size as booked: the server sizes a booking sent without them as 0.
      vm_count: b.vm_count, vcpu_total: b.vcpu_total, ram_mb_total: b.ram_mb_total, disk_gb_total: b.disk_gb_total,
      template_id: b.template_id, range_id: b.range_id,
      instructor_id: b.instructor_id ?? null, course_id: b.course_id ?? null, scenario_id: b.scenario_id ?? null,
      exercise_id: b.exercise_id ?? null, class_id: b.class_id ?? null,
      range_mode: (b.class_id && b.range_mode ? b.range_mode : null) as BookingIn['range_mode'],
      draft: b.state === 'draft',
    };
    this.run(this.scheduler.update(b.id, body), r =>
      r.state === 'pending_approval' && b.state !== 'pending_approval' ? `${r.name} moved and sent for approval` : `${r.name} moved`);
  }

  acceptProposal(b: Booking): void {
    this.run(this.scheduler.acceptProposal(b.id), r => `${r.name}: proposed time accepted`);
  }
  declineProposal(b: Booking): void {
    this.run(this.scheduler.declineProposal(b.id), r => `${r.name}: proposed time declined`);
  }

  private run(call: Observable<Booking>, done: (b: Booking) => string): void {
    this.busy.set(true);
    this.actionError.set('');
    call.subscribe({
      next: r => {
        this.busy.set(false);
        this.moving.set(null);
        this.notify.success(done(r));
        this.load();
      },
      error: (e: HttpErrorResponse) => {
        this.busy.set(false);
        this.actionError.set(approvalError(e));
      },
    });
  }

  // ── view helpers ──
  kind(b: Booking): EventKind { return kindOf(b); }
  kindLabel(b: Booking): string { return KIND_STYLE[kindOf(b)].label; }
  colour(b: Booking): string { return KIND_STYLE[kindOf(b)].colour; }
  icon(b: Booking): string { return KIND_STYLE[kindOf(b)].icon; }
  classes(b: Booking): string { return blockClasses(b); }
  pending(b: Booking): boolean { return isPending(b); }
  lane(e: Block): { left: string; width: string } { return laneStyle(e.lane, e.lanes); }
  pad(h: number): string { return String(h).padStart(2, '0'); }
  hhmmOf(iso: string): string { return hhmm(new Date(iso)); }
  gbOf(mb: number): string { return gb(mb); }
  time(b: Booking): string { return `${this.hhmmOf(b.start_time)}–${this.hhmmOf(b.end_time)}`; }
  when(b: Booking): string { return whenLabel(b.start_time, b.end_time); }
  whenOf(start: string, end: string): string { return whenLabel(start, end); }
  ariaLabel(b: Booking): string {
    return `${KIND_STYLE[kindOf(b)].label}: ${b.name}, ${this.time(b)}${isPending(b) ? ', awaiting approval' : ''}`;
  }
  stateLabel(s: string): string {
    if (s === 'pending_approval') return 'Awaiting approval';
    if (HOLDING_STATES.includes(s) && s !== 'scheduled') return s === 'active' ? 'Running' : 'Building';
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  className(b: Booking): string {
    return this.classList().find(c => c.id === b.class_id)?.name ?? 'A class';
  }
  personName(id: string): string {
    const me = this.auth.user();
    if (me && me.id === id) return me.display_name;
    return this.people().find(u => u.id === id)?.display_name ?? 'Instructor';
  }
}
