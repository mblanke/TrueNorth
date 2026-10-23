import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { ApiService } from '@core/services/api.service';
import {
  ContentItem, ContentItemDetail, ContentReviewApiService, ContentTheme,
} from '@core/services/content-review-api.service';
import { NotificationService } from '@core/services/notification.service';
import type { User } from '@core/models';
import { apiError } from '../ranges/range-capabilities';
import {
  CalendarView, NO_THEME_COLOUR, addDays, clockText, dayKey, gridDays, heading, kindLabel, listText, shift,
  themeOf,
} from './content-calendar.util';

interface Day {
  key: string;
  label: string;
  inPeriod: boolean;
  today: boolean;
  items: ContentItem[];
}

/**
 * The calendar's Content view (ADR 0014 decisions 22-24, issue #286): all content on its
 * review-due date, coloured by theme, STALE items outlined in red with a clock; the
 * "Stale now" list; the selected item's clock, cycle, last review, what it depends on and
 * what uses it, and Mark reviewed / Assign reviewer / Retire. The Events tab is the
 * existing schedule for now (its redesign is #285).
 */
@Component({
  selector: 'tn-content-calendar',
  imports: [
    DatePipe, FormsModule, RouterLink, MatButtonModule, MatButtonToggleModule, MatFormFieldModule, MatIconModule,
    MatInputModule, MatSelectModule,
  ],
  template: `
    <div class="page-container content-cal">
      <header class="cal-header">
        <div class="titles">
          <div class="eyebrow">Content review</div>
          <h1>{{ title() }}</h1>
        </div>
        <div class="controls">
          <nav class="segmented" aria-label="Calendar views">
            <a routerLink="/schedule">Events</a>
            <span class="on" aria-current="page">Content</span>
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
        </div>
      </header>

      <div class="chips" role="group" aria-label="Themes">
        @for (t of themes(); track t.id) {
          <button type="button" class="chip" [class.off]="off().has(t.id)" [attr.aria-pressed]="!off().has(t.id)"
                  [style.--c]="t.colour" (click)="toggleTheme(t.id)">
            <span class="dot"></span>{{ t.name }}<span class="count">{{ counts()[t.id] ?? 0 }}</span>
          </button>
        }
        @if ((counts()['none'] ?? 0) > 0) {
          <button type="button" class="chip" [class.off]="off().has('none')" [attr.aria-pressed]="!off().has('none')"
                  [style.--c]="noTheme" (click)="toggleTheme('none')">
            <span class="dot"></span>No theme<span class="count">{{ counts()['none'] }}</span>
          </button>
        }
      </div>

      @if (loadError()) {
        <p class="error" role="alert">{{ loadError() }}</p>
      }

      <div class="layout">
        <section class="grid-card" aria-label="Review dates">
          <div class="grid" [class.week]="view() === 'week'">
            @for (d of weekdays; track d) { <div class="dow">{{ d }}</div> }
            @for (day of days(); track day.key) {
              <div class="cell" [class.out]="!day.inPeriod">
                <div class="num" [class.today]="day.today">{{ day.label }}</div>
                @for (it of day.items; track it.id) {
                  <button type="button" class="item" [class.stale]="it.stale" [class.selected]="it.id === selectedId()"
                          [style.--c]="colour(it)" (click)="select(it.id)"
                          [attr.aria-label]="(it.stale ? 'Stale: ' : '') + it.title">
                    <span class="dot"></span><span class="t">{{ it.title }}</span>
                    @if (it.stale) { <mat-icon class="clock" aria-hidden="true">schedule</mat-icon> }
                  </button>
                }
              </div>
            }
          </div>
        </section>

        <aside class="side">
          <section class="card" aria-label="Stale now">
            <div class="card-head">
              <h2>Stale now</h2><span class="badge">{{ staleShown().length }}</span>
            </div>
            @for (s of staleShown(); track s.id) {
              <button type="button" class="stale-row" [style.--c]="colour(s)" (click)="select(s.id)">
                <span class="row-title"><span class="dot"></span>{{ s.title }}</span>
                <span class="reason">{{ s.stale_reasons?.[0]?.text }}</span>
              </button>
            } @empty {
              <p class="muted">Nothing is stale.</p>
            }
          </section>

          <section class="card" aria-label="Selected content">
            @if (detail(); as sel) {
              <div class="tags">
                <span class="tag" [style.--c]="colour(sel)">{{ themeName(sel) }}</span>
                <span class="kind">{{ kind(sel.item_type) }}</span>
                @if (sel.shared) { <span class="kind">Shared</span> }
              </div>
              <h2 class="sel-title">{{ sel.title }}</h2>
              <div class="clock-badge" [class]="'clock-badge ' + sel.clock.state">{{ clockText(sel) }}</div>
              @if ((sel.stale_reasons?.length ?? 0) > 1) {
                <ul class="reasons">
                  @for (r of (sel.stale_reasons ?? []).slice(1); track $index) { <li>{{ r.text }}</li> }
                </ul>
              }
              <dl class="facts">
                <dt>Review cycle</dt><dd>Every {{ sel.cycle_days }} days</dd>
                <dt>Last review</dt>
                <dd>{{ sel.last_reviewed_at ? (sel.last_reviewed_at | date: 'd MMM y') : 'Never' }}
                  @if (sel.last_reviewed_by_name) { by {{ sel.last_reviewed_by_name }} }</dd>
                <dt>Due</dt><dd>{{ sel.due_at | date: 'd MMM y' }}</dd>
                <dt>Owner</dt><dd>{{ sel.owner_name ?? 'Nobody yet' }}</dd>
                <dt>Reviewer</dt><dd>{{ sel.reviewer_name ?? 'Nobody' }}</dd>
                <dt>Depends on</dt><dd>{{ list(sel.depends_on ?? []) }}</dd>
                <dt>Used by</dt><dd>{{ list(sel.used_by ?? []) }}</dd>
              </dl>

              @if (assigning()) {
                <div class="inline-form">
                  <mat-form-field appearance="outline" subscriptSizing="dynamic">
                    <mat-label>Reviewer</mat-label>
                    <mat-select [(ngModel)]="reviewerChoice">
                      <mat-option [value]="null">Nobody</mat-option>
                      @for (u of staff(); track u.id) { <mat-option [value]="u.id">{{ u.display_name }}</mat-option> }
                    </mat-select>
                  </mat-form-field>
                  <button mat-flat-button (click)="assign()" [disabled]="busy()">Assign</button>
                  <button mat-button (click)="assigning.set(false)">Cancel</button>
                </div>
              }
              @if (retiring()) {
                <div class="inline-form">
                  <mat-form-field appearance="outline" subscriptSizing="dynamic">
                    <mat-label>Why retire it?</mat-label>
                    <input matInput [(ngModel)]="retireReason" maxlength="500">
                  </mat-form-field>
                  <button mat-flat-button class="danger" (click)="retire()"
                          [disabled]="busy() || !retireReason.trim()">Retire now</button>
                  <button mat-button (click)="retiring.set(false)">Cancel</button>
                </div>
              }
              @if (actionError()) { <p class="error" role="alert">{{ actionError() }}</p> }
              <div class="actions">
                <button mat-flat-button (click)="markReviewed()" [disabled]="busy() || !sel.can_review">
                  Mark reviewed
                </button>
                <button mat-stroked-button (click)="startAssign()" [disabled]="busy() || !sel.can_assign">
                  Assign reviewer
                </button>
                <button mat-button class="retire" (click)="retiring.set(true)"
                        [disabled]="busy() || !sel.can_admin || sel.retired">Retire</button>
              </div>
            } @else {
              <p class="muted">Select an item to see its review clock.</p>
            }
          </section>
        </aside>
      </div>
    </div>
  `,
  styles: [`
    .content-cal { display: flex; flex-direction: column; gap: 20px; }
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
    .chips { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .chip { display: inline-flex; align-items: center; gap: 8px; min-height: 38px; padding: 0 14px; border-radius: 19px;
      font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; border: 0;
      background: color-mix(in srgb, var(--c) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c) 65%, var(--mat-sys-on-surface)); }
    .chip.off { background: var(--mat-sys-surface); color: var(--mat-sys-on-surface-variant);
      border: 1px solid var(--mat-sys-outline-variant); }
    .chip .count { font-size: 12px; font-weight: 500; opacity: 0.7; }
    .dot { width: 8px; height: 8px; border-radius: 4px; flex: none; background: var(--c); }
    .chip.off .dot { background: var(--mat-sys-outline-variant); }
    .layout { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start; }
    .grid-card { flex: 999 1 760px; min-width: 0; overflow-x: auto; background: var(--mat-sys-surface);
      border-radius: 18px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .grid { min-width: 900px; padding: 8px; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
    .dow { padding: 12px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-outline); }
    .cell { min-height: 108px; box-sizing: border-box; padding: 8px; border-radius: 12px;
      border: 1px solid var(--mat-sys-outline-variant); background: var(--mat-sys-surface); }
    .grid.week .cell { min-height: 520px; }
    .cell.out { background: var(--mat-sys-surface-container-low); }
    .cell.out .num { color: var(--mat-sys-outline); }
    .num { font-size: 12px; font-weight: 600; }
    .num.today { display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 24px;
      padding: 0 6px; box-sizing: border-box; border-radius: 12px; background: var(--mat-sys-primary);
      color: var(--mat-sys-on-primary); }
    .item { display: flex; align-items: center; gap: 6px; width: 100%; text-align: left; margin-top: 6px; padding: 5px 8px;
      border-radius: 8px; font: inherit; font-size: 11px; font-weight: 600; line-height: 1.3; cursor: pointer; border: 0;
      background: color-mix(in srgb, var(--c) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c) 65%, var(--mat-sys-on-surface)); }
    .item .dot { width: 6px; height: 6px; border-radius: 3px; }
    .item .t { flex: 1; min-width: 0; }
    .item.stale { outline: 1.5px solid #F04438; outline-offset: -1.5px; }
    .item.selected { box-shadow: 0 0 0 2px var(--mat-sys-primary); }
    .item .clock { font-size: 14px; width: 14px; height: 14px; color: #B42318; flex: none; }
    .side { flex: 1 1 320px; min-width: 280px; display: flex; flex-direction: column; gap: 16px; }
    .card { background: var(--mat-sys-surface); border-radius: 18px; padding: 18px; display: flex; flex-direction: column;
      gap: 10px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .card-head { display: flex; align-items: center; justify-content: space-between; }
    .card-head h2 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-on-surface-variant); }
    .badge { background: #FEE4E2; color: #B42318; border-radius: 10px; padding: 2px 10px; font-size: 12px; font-weight: 700; }
    .stale-row { text-align: left; border: 0; font: inherit; cursor: pointer; min-height: 44px; border-radius: 14px;
      padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; background: var(--mat-sys-surface-container-low); }
    .row-title { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600;
      color: var(--mat-sys-on-surface); }
    .reason { font-size: 12px; color: #B42318; padding-left: 16px; }
    .tags { display: flex; gap: 8px; flex-wrap: wrap; }
    .tag { padding: 3px 10px; border-radius: 8px; font-size: 12px; font-weight: 600;
      background: color-mix(in srgb, var(--c) 14%, var(--mat-sys-surface));
      color: color-mix(in srgb, var(--c) 65%, var(--mat-sys-on-surface)); }
    .kind { padding: 3px 10px; border-radius: 8px; font-size: 12px; font-weight: 600;
      background: var(--mat-sys-surface-container-high); color: var(--mat-sys-on-surface-variant); }
    .sel-title { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
    .clock-badge { font-size: 13px; font-weight: 600; border-radius: 10px; padding: 8px 12px; }
    .clock-badge.green { background: #ECFDF3; color: #067647; }
    .clock-badge.amber { background: #FFFAEB; color: #B54708; }
    .clock-badge.stale { background: #FEF3F2; color: #B42318; }
    .reasons { margin: 0; padding-left: 18px; font-size: 12px; color: #B42318; }
    .facts { display: grid; grid-template-columns: 92px 1fr; row-gap: 8px; column-gap: 12px; font-size: 13px; margin: 0; }
    .facts dt { color: var(--mat-sys-on-surface-variant); }
    .facts dd { margin: 0; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
    .retire, .danger { color: #B42318; }
    .inline-form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; margin: 0; }
    .error { color: var(--mat-sys-error); font-size: 13px; margin: 0; }
  `],
})
export class ContentCalendarComponent implements OnInit {
  private readonly api = inject(ContentReviewApiService);
  private readonly core = inject(ApiService);
  private readonly notify = inject(NotificationService);

  readonly weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  readonly noTheme = NO_THEME_COLOUR;
  readonly clockText = clockText;
  readonly kind = kindLabel;
  readonly list = listText;

  readonly view = signal<CalendarView>('month');
  readonly anchor = signal(new Date());
  readonly themes = signal<ContentTheme[]>([]);
  readonly off = signal<ReadonlySet<string>>(new Set());
  readonly items = signal<ContentItem[]>([]);
  readonly counts = signal<Record<string, number>>({});
  readonly stale = signal<ContentItem[]>([]);
  readonly selectedId = signal<string | null>(null);
  readonly detail = signal<ContentItemDetail | null>(null);
  readonly staff = signal<User[]>([]);
  readonly assigning = signal(false);
  readonly retiring = signal(false);
  readonly busy = signal(false);
  readonly loadError = signal('');
  readonly actionError = signal('');
  reviewerChoice: string | null = null;
  retireReason = '';

  private readonly themeMap = computed(() => new Map(this.themes().map(t => [t.id, t])));
  readonly title = computed(() => heading(this.anchor(), this.view()));

  readonly days = computed<Day[]>(() => {
    const anchor = this.anchor();
    const view = this.view();
    const todayKey = dayKey(new Date());
    const byDay = new Map<string, ContentItem[]>();
    for (const it of this.items()) {
      if (!this.shown(it)) continue;
      const k = dayKey(new Date(it.due_at));
      byDay.set(k, [...(byDay.get(k) ?? []), it]);
    }
    return gridDays(anchor, view).map(d => {
      const key = dayKey(d);
      return {
        key,
        label: d.getDate() === 1 ? `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })}` : String(d.getDate()),
        inPeriod: view === 'week' || d.getMonth() === anchor.getMonth(),
        today: key === todayKey,
        items: byDay.get(key) ?? [],
      };
    });
  });

  readonly staleShown = computed(() => this.stale().filter(s => this.shown(s)));

  ngOnInit(): void {
    this.api.themes().subscribe({ next: t => this.themes.set(t), error: e => this.loadError.set(apiError(e)) });
    this.loadWindow();
    this.loadStale(true);
  }

  // ── navigation ──
  setView(view: CalendarView): void {
    this.view.set(view);
    this.loadWindow();
  }
  move(dir: -1 | 1): void {
    this.anchor.set(shift(this.anchor(), this.view(), dir));
    this.loadWindow();
  }
  today(): void {
    this.anchor.set(new Date());
    this.loadWindow();
  }
  toggleTheme(id: string): void {
    const next = new Set(this.off());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.off.set(next);
  }

  colour(item: Pick<ContentItem, 'theme_ids'>): string {
    return themeOf(item, this.themeMap())?.colour ?? NO_THEME_COLOUR;
  }
  themeName(item: Pick<ContentItem, 'theme_ids'>): string {
    return themeOf(item, this.themeMap())?.name ?? 'No theme';
  }

  private shown(item: Pick<ContentItem, 'theme_ids'>): boolean {
    const off = this.off();
    if (!off.size) return true;
    const ids = item.theme_ids?.length ? item.theme_ids : ['none'];
    return ids.some(id => !off.has(id));
  }

  // ── loading ──
  private loadWindow(): void {
    const days = gridDays(this.anchor(), this.view());
    this.api.list(days[0], addDays(days[days.length - 1], 1)).subscribe({
      next: res => {
        this.items.set(res.items);
        this.counts.set(res.theme_counts);
        this.loadError.set('');
      },
      error: e => this.loadError.set(apiError(e)),
    });
  }

  private loadStale(selectFirst = false): void {
    this.api.stale().subscribe({
      next: s => {
        this.stale.set(s);
        if (selectFirst && !this.selectedId() && s.length) this.select(s[0].id);
      },
      error: e => this.loadError.set(apiError(e)),
    });
  }

  select(id: string): void {
    this.selectedId.set(id);
    this.assigning.set(false);
    this.retiring.set(false);
    this.actionError.set('');
    this.api.get(id).subscribe({ next: d => this.detail.set(d), error: e => this.actionError.set(apiError(e)) });
  }

  // ── actions ──
  markReviewed(): void {
    const sel = this.detail();
    if (!sel) return;
    this.run(this.api.markReviewed(sel.id), `${sel.title} marked reviewed`);
  }

  startAssign(): void {
    const sel = this.detail();
    this.reviewerChoice = sel?.reviewer_id ?? null;
    this.assigning.set(true);
    this.retiring.set(false);
    if (!this.staff().length) {
      this.core.listUsers().subscribe({
        next: users => this.staff.set(users.filter(u => u.role === 'admin' || u.role === 'instructor')),
        error: e => this.actionError.set(apiError(e)),
      });
    }
  }

  assign(): void {
    const sel = this.detail();
    if (!sel) return;
    this.run(this.api.assignReviewer(sel.id, this.reviewerChoice), 'Reviewer assigned');
  }

  retire(): void {
    const sel = this.detail();
    if (!sel || !this.retireReason.trim()) return;
    this.run(this.api.retire(sel.id, this.retireReason.trim()), `${sel.title} retired`);
  }

  private run(call: ReturnType<ContentReviewApiService['get']>, done: string): void {
    this.busy.set(true);
    this.actionError.set('');
    call.subscribe({
      next: d => {
        this.busy.set(false);
        this.detail.set(d);
        this.assigning.set(false);
        this.retiring.set(false);
        this.retireReason = '';
        this.notify.success(done);
        this.loadWindow();
        this.loadStale();
      },
      error: e => {
        this.busy.set(false);
        this.actionError.set(apiError(e));
      },
    });
  }
}
