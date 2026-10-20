import {
  AfterViewInit, ChangeDetectionStrategy, Component, DestroyRef, ElementRef, OnInit, computed, inject, input, output,
  signal, viewChild,
} from '@angular/core';
import { DOCUMENT, DatePipe } from '@angular/common';
import { A11yModule } from '@angular/cdk/a11y';
import { CdkPortal, DomPortalOutlet, PortalModule } from '@angular/cdk/portal';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog } from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { HttpErrorResponse } from '@angular/common/http';
import { ApiService, ClassStudent, ExerciseClass } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import type { InjectRecord } from '@core/models';
import { ConfirmDialogComponent } from '../../../shared/components/confirm-dialog/confirm-dialog.component';
import { objectiveName } from '../../../shared/objective-display';
import { InjectTimelineComponent } from './inject-timeline.component';
import {
  LANES, LaneKey, OBJECTIVE_STATE_WORD, RESETTABLE_LAB, STATUS_WORD, SortKey, StatusKey, VERDICT_WORD, ago, lanesOf,
  sortStudents, statusesOf, tplus,
} from './live-class';

const REFRESH_MS = 30_000;
export const F2_SOON = 'Coming in R1 F2 (intervention console)';

/**
 * The live class (issue #199, layout B): attention lanes above a sortable roster, a Student
 * drawer, and the inject timeline across the top. Everything shown comes from
 * GET /ops/exercises/{id}/class and GET /exercises/{id}/injects. The only action that changes
 * anything is "Reset lab", which exists in the API (POST /lab-sessions/{id}/reset), is
 * confirmed first, and shows "Resetting" until the API answers.
 */
@Component({
  selector: 'tn-live-class',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, A11yModule, PortalModule, MatButtonModule, MatIconModule, MatTooltipModule, InjectTimelineComponent],
  template: `
    <tn-inject-timeline [injects]="injects()" [startedAt]="cls()?.started_at" [endAt]="axisEnd()" />

    @if (loading() && !cls()) {
      <p class="state-line" role="status">Loading the class…</p>
    } @else if (error() && !cls()) {
      <div class="state-line error" role="alert">
        {{ error() }}
        <button mat-button type="button" (click)="refresh()">Try again</button>
      </div>
    } @else if (cls(); as c) {
      @if (error()) {
        <p class="state-line stale" role="status">Could not refresh ({{ error() }}). Showing the class as of {{ c.generated_at | date:'shortTime' }}.</p>
      }
      <p class="meta" data-testid="class-meta">
        <span>Roster <strong>{{ students().length }} {{ students().length === 1 ? 'Student' : 'Students' }}</strong>@if (teamCount()) { · {{ teamCount() }} {{ teamCount() === 1 ? 'team' : 'teams' }}}</span>
        @if (labCount()) { <span>In a ready lab <strong>{{ labsUp() }} of {{ labCount() }}</strong></span> }
        <span>Credited <strong>{{ creditedTotal() }} of {{ students().length * (c.objectives?.length ?? 0) }}</strong> Student objectives</span>
      </p>

      <!-- Action queue: four lanes, never merged (R1 F2). -->
      <div class="queue" role="region" aria-label="Needs attention">
        @for (lane of laneMeta; track lane.key) {
          <section class="qcol" [class]="'qcol ' + lane.key" [attr.aria-labelledby]="'lane-' + lane.key" [attr.data-testid]="'lane-' + lane.key">
            <div class="lane-title">
              <h2 [id]="'lane-' + lane.key">{{ lane.title }}</h2>
              @if (lane.key !== 'help') { <span class="chip" [attr.aria-label]="lanes()[lane.key].length + ' in this lane'">{{ lanes()[lane.key].length }}</span> }
            </div>
            <p class="lane-hint">{{ lane.hint }}</p>
            @if (lane.key === 'lab' && rangeFault(); as rf) {
              <div class="qcard exercise-fault">
                <strong>Exercise range {{ rf.state }}</strong>
                <p>{{ rf.error || 'The shared range reports a failure.' }} This is the exercise's, not any Student's.</p>
              </div>
            }
            @for (it of lanes()[lane.key]; track it.student.user_id + it.item.kind + (it.item.objective_ref ?? '')) {
              <div class="qcard">
                <div class="top">
                  <button type="button" class="link name" (click)="open(it.student, $event)">{{ it.student.display_name }}</button>
                  @if (it.student.team) { <span class="small muted">{{ it.student.team.name }}</span> }
                </div>
                <p>{{ detailText(it.item) }}@if (it.item.since) { <span class="muted"> · since {{ tp(it.item.since) || (it.item.since | date:'shortTime') }}</span>}</p>
                <div class="acts">
                  @if (lane.key === 'learning') {
                    <button mat-stroked-button type="button" class="soon" aria-disabled="true" [matTooltip]="soon"
                            [attr.aria-label]="'Message ' + it.student.display_name + ' (' + soon + ')'">Message</button>
                  }
                  <button mat-button type="button" (click)="open(it.student, $event)" [attr.aria-label]="'Open ' + it.student.display_name">Open</button>
                </div>
              </div>
            } @empty {
              @if (lane.key !== 'help' && !(lane.key === 'lab' && rangeFault())) { <p class="lane-empty">Nobody</p> }
            }
          </section>
        }
      </div>

      <!-- Roster -->
      <section class="roster" aria-labelledby="roster-title">
        <div class="tbl-bar"><h2 id="roster-title">Roster</h2></div>
        @if (!students().length) {
          <p class="empty" data-testid="roster-empty">
            No Students on this exercise yet. The roster is the course booked over this exercise (or its range), the
            exercise's teams, Students with their own lab on its range, and anyone who submitted a detection in this run.
          </p>
        } @else {
          <div class="tbl-wrap">
            <table class="dense">
              <caption class="sr-only">Students in {{ c.exercise_name }}. Column headers sort the table.</caption>
              <thead>
                <tr>
                  @for (col of columns; track col.key) {
                    <th scope="col" [class]="col.cls" [attr.aria-sort]="sortKey() === col.key ? (sortDir() === 'asc' ? 'ascending' : 'descending') : 'none'">
                      <button type="button" class="sort" (click)="sortBy(col.key)">
                        {{ col.label }}
                        @if (sortKey() === col.key) { <mat-icon aria-hidden="true">{{ sortDir() === 'asc' ? 'arrow_upward' : 'arrow_downward' }}</mat-icon> }
                      </button>
                    </th>
                  }
                  <th scope="col" class="c-obj">Working on <span class="muted small">(inferred)</span></th>
                  <th scope="col" class="c-score">Score</th>
                </tr>
              </thead>
              <tbody>
                @for (s of sorted(); track s.user_id) {
                  <tr tabindex="0" (click)="open(s, $event)" (keydown.enter)="$event.preventDefault(); open(s, $event)" (keydown.space)="$event.preventDefault(); open(s, $event)"
                      [attr.aria-label]="s.display_name + ', ' + statusText(s) + '. Open details'">
                    <td><span class="name">{{ s.display_name }}</span></td>
                    <td class="c-team">
                      @if (s.team) { <span class="team-dot" aria-hidden="true" [style.background]="s.team.color_hex || 'var(--border-strong)'"></span>{{ s.team.name }} }
                      @else { <span class="muted">No team</span> }
                    </td>
                    <td>
                      @for (k of statuses(s); track k) { <span [class]="'st ' + k">{{ word(k) }}</span> }
                    </td>
                    <td>{{ s.attempts_credited }} / {{ s.attempts_total }}</td>
                    <td class="c-last small">{{ agoText(s.last_activity_at) }}</td>
                    <td class="c-obj">{{ s.current_objective ? objName(s.current_objective) : 'All credited' }}</td>
                    <td class="c-score">{{ s.score }} / {{ c.max_score }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
      </section>
    }

    <!-- Student drawer: rendered at the end of <body> (cdkPortal), so it covers the viewport. -->
    <ng-template cdkPortal>
    @if (selected(); as s) {
      <div class="scrim" (click)="close()" aria-hidden="true"></div>
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="dr-name" cdkTrapFocus cdkTrapFocusAutoCapture
             (keydown.escape)="close()" data-testid="student-drawer">
        <div class="dr-head">
          <div>
            <div class="small muted">
              @if (s.team) { <span class="team-dot" aria-hidden="true" [style.background]="s.team.color_hex || 'var(--border-strong)'"></span>{{ s.team.name }} · }Student
            </div>
            <h2 id="dr-name">{{ s.display_name }}</h2>
            <div class="flags">@for (k of statuses(s); track k) { <span [class]="'st ' + k">{{ word(k) }}</span> }</div>
          </div>
          <button mat-icon-button type="button" #closeBtn (click)="close()" [attr.aria-label]="'Close ' + s.display_name" cdkFocusInitial>
            <mat-icon>close</mat-icon>
          </button>
        </div>
        <div class="dr-body">
          @for (a of s.attention ?? []; track $index) {
            <div class="att-box" [class]="'att-box ' + a.kind">{{ detailText(a) }}</div>
          }
          <section class="dr-sec" aria-labelledby="dr-lab">
            <h3 id="dr-lab">Lab</h3>
            @if (s.lab; as lab) {
              <dl class="kv">
                <dt>State</dt><dd>{{ lab.state }}@if (resetting() === lab.session_id) { (reset requested)}</dd>
                <dt>Attempt</dt><dd>{{ lab.attempt }}</dd>
                <dt>Last heartbeat</dt><dd>{{ agoText(lab.last_seen_at) }}</dd>
                <dt>Idle timeout</dt><dd>{{ lab.idle_expires_at ? (lab.idle_expires_at | date:'shortTime') : '—' }}</dd>
                <dt>Ends at</dt><dd>{{ lab.max_expires_at ? (lab.max_expires_at | date:'shortTime') : '—' }}</dd>
              </dl>
              @if (lab.error) { <p class="fault-box"><strong>Infrastructure fault.</strong> {{ lab.error }} Not counted against the Student.</p> }
            } @else {
              <p class="muted small">No lab of their own on this exercise's range; they work on the exercise's range.</p>
            }
          </section>
          <section class="dr-sec" aria-labelledby="dr-obj">
            <h3 id="dr-obj">Objectives <span class="small muted">{{ s.score }} / {{ cls()?.max_score }} pts</span></h3>
            <ul class="obj-list">
              @for (o of s.objectives ?? []; track o.ref_id) {
                <li>
                  <span>{{ objName(o.ref_id) }} <span class="muted small">· {{ points(o.ref_id) }} pts</span></span>
                  <span [class]="'v ' + o.state">{{ objWord(o.state) }}@if (o.misses && o.state !== 'credited') { · {{ o.misses }} missed}</span>
                </li>
              }
            </ul>
          </section>
          <section class="dr-sec" aria-labelledby="dr-att">
            <h3 id="dr-att">Detection attempts <span class="small muted">{{ s.attempts_total }}</span></h3>
            @if ((s.attempts ?? []).length) {
              <table class="att">
                <thead><tr><th scope="col">When</th><th scope="col">Objective</th><th scope="col">Verdict</th><th scope="col">Matched</th></tr></thead>
                <tbody>
                  @for (a of attemptsNewestFirst(s); track a.id) {
                    <tr>
                      <td>{{ tp(a.submitted_at) || (a.submitted_at | date:'shortTime') }}</td>
                      <td>{{ objName(a.objective_ref) }}</td>
                      <td [class]="'v ' + a.verdict">{{ verdictWord(a.verdict) }}</td>
                      <td>{{ a.events_matched ?? '—' }}@if (a.precision !== null && a.precision !== undefined) { · {{ (a.precision * 100).toFixed(0) }}% on target}</td>
                    </tr>
                    @if (a.query) { <tr class="q"><td></td><td colspan="3"><code>{{ a.query }}</code></td></tr> }
                  }
                </tbody>
              </table>
            } @else {
              <p class="muted small">No detections submitted in this run yet.</p>
            }
          </section>
          <section class="dr-sec" aria-labelledby="dr-int">
            <h3 id="dr-int">Help requests and interventions</h3>
            <p class="muted small">These arrive with the intervention console (R1 F2).</p>
          </section>
        </div>
        <div class="dr-actions">
          <button mat-stroked-button type="button" class="soon" aria-disabled="true" [matTooltip]="soon" [attr.aria-label]="'Message (' + soon + ')'">Message</button>
          <button mat-stroked-button type="button" class="soon" aria-disabled="true" [matTooltip]="soon" [attr.aria-label]="'Extend lab time (' + soon + ')'">Extend lab time</button>
          @if (canResetLab() && s.lab; as lab) {
            @if (resetting() === lab.session_id) {
              <button mat-flat-button color="warn" type="button" disabled>Resetting…</button>
            } @else if (resettable(lab.state)) {
              <button mat-flat-button color="warn" type="button" (click)="confirmReset(s)">Reset lab…</button>
            } @else {
              <button mat-stroked-button type="button" class="soon" aria-disabled="true"
                      [matTooltip]="'A lab can be reset once it is ready; this one is ' + lab.state">Reset lab…</button>
            }
          }
        </div>
      </aside>
    }
    </ng-template>
  `,
  styles: [`
    :host { display: block; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    .state-line { padding: 12px 0; color: var(--text-secondary); }
    .state-line.error { color: var(--severity-critical, var(--accent)); display: flex; align-items: center; gap: 8px; }
    .state-line.stale { color: var(--warning); font-size: 13px; }
    .meta { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--text-secondary); margin: 0 0 12px; }
    .meta strong { color: var(--text-primary); }
    .muted { color: var(--text-muted); }
    .small { font-size: 12px; }
    .name { font-weight: 600; }
    .link { background: none; border: 0; padding: 0; color: inherit; font: inherit; cursor: pointer; text-align: left; }
    .link:hover, .link:focus-visible { text-decoration: underline; text-underline-offset: 3px; }
    h2 { font-size: 14px; margin: 0; }
    h3 { font-size: 13px; margin: 0 0 8px; display: flex; justify-content: space-between; align-items: baseline; }

    .queue { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .qcol { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md, 8px); padding: 10px 12px; min-height: 110px; border-top-width: 3px; }
    .qcol.help { border-top-color: var(--accent); }
    .qcol.lab { border-top-color: var(--warning); }
    .qcol.learning { border-top-color: var(--severity-low); }
    .qcol.quiet { border-top-color: var(--border-strong, var(--border)); }
    .lane-title { display: flex; justify-content: space-between; align-items: center; }
    .lane-hint { font-size: 11.5px; color: var(--text-muted); margin: 4px 0 6px; }
    .lane-empty { font-size: 12px; color: var(--text-muted); margin: 2px 0; }
    .chip { font-size: 11px; padding: 1px 8px; border-radius: 999px; background: var(--bg-surface); color: var(--text-secondary); }
    .qcard { border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); padding: 8px 10px; margin-top: 8px; background: var(--bg-secondary); }
    .qcard .top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
    .qcard p { margin: 4px 0 6px; font-size: 13px; }
    .qcard .acts { display: flex; gap: 6px; flex-wrap: wrap; }
    .qcard.exercise-fault { border-left: 3px solid var(--warning); }
    .soon { opacity: .6; cursor: not-allowed; }

    .roster { container-type: inline-size; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md, 8px); }
    .tbl-bar { padding: 12px 16px; border-bottom: 1px solid var(--border); }
    .tbl-wrap { overflow-x: auto; }
    .empty { padding: 16px; color: var(--text-secondary); font-size: 13px; margin: 0; }
    table.dense { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.dense th { text-align: left; font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
    th .sort { background: none; border: 0; padding: 0; font: inherit; color: inherit; text-transform: inherit; letter-spacing: inherit; cursor: pointer; display: inline-flex; align-items: center; gap: 2px; }
    th .sort mat-icon { font-size: 14px; width: 14px; height: 14px; }
    table.dense td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    table.dense tbody tr { cursor: pointer; }
    table.dense tbody tr:hover, table.dense tbody tr:focus-visible { background: var(--bg-secondary); }
    table.dense tbody tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
    .team-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; margin-right: 6px; vertical-align: middle; }

    /* Status: shape + words, never colour alone. Red stays reserved for help requests. */
    .st { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 550; white-space: nowrap; margin-right: 10px; }
    .st::before { content: ""; width: 8px; height: 8px; border-radius: 50%; flex: none; background: currentColor; }
    .st.lab_fault { color: var(--warning); }
    .st.lab_fault::before { border-radius: 2px; }
    .st.lab_wait { color: var(--warning); }
    .st.lab_wait::before { background: transparent; border: 2px solid currentColor; width: 6px; height: 6px; }
    .st.learning { color: var(--severity-low); }
    .st.learning::before { border-radius: 1px; transform: rotate(45deg); }
    .st.not_seen { color: var(--text-muted); }
    .st.not_seen::before { background: transparent; border: 2px dotted currentColor; width: 6px; height: 6px; }
    .st.ok { color: var(--success); }

    .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, .32); z-index: 1000; }
    .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 100vw); z-index: 1001; background: var(--bg-card); color: var(--text-primary); border-left: 1px solid var(--border); display: flex; flex-direction: column; box-shadow: var(--shadow-2, 0 8px 28px rgba(0,0,0,.3)); }
    @media (prefers-reduced-motion: no-preference) { .drawer { animation: tn-slide-in .18s ease-out; } }
    @keyframes tn-slide-in { from { transform: translateX(24px); opacity: .6; } to { transform: none; opacity: 1; } }
    .dr-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; padding: 16px 20px 12px; border-bottom: 1px solid var(--border); }
    .dr-head h2 { font-size: 19px; margin: 2px 0 6px; }
    .flags { display: flex; flex-wrap: wrap; gap: 4px; }
    .dr-body { flex: 1; overflow-y: auto; padding: 8px 20px 16px; }
    .dr-sec { padding: 12px 0; border-bottom: 1px solid var(--border); }
    .dr-sec:last-child { border-bottom: 0; }
    .att-box { margin-top: 8px; padding: 8px 12px; border-left: 3px solid var(--border-strong, var(--border)); background: var(--bg-secondary); font-size: 13px; border-radius: 0 6px 6px 0; }
    .att-box.lab_fault, .att-box.lab_wait { border-left-color: var(--warning); }
    .att-box.learning { border-left-color: var(--severity-low); }
    .kv { display: grid; grid-template-columns: 130px 1fr; gap: 4px 12px; font-size: 13px; margin: 0; }
    .kv dt { color: var(--text-muted); }
    .kv dd { margin: 0; }
    .fault-box { border-left: 3px solid var(--warning); background: var(--warn-soft, var(--bg-secondary)); padding: 8px 12px; font-size: 13px; margin: 8px 0 0; }
    .obj-list { list-style: none; margin: 0; padding: 0; }
    .obj-list li { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; font-size: 13px; border-bottom: 1px dashed var(--border); }
    .obj-list li:last-child { border-bottom: 0; }
    .v { font-weight: 550; font-size: 12px; }
    .v.credited, .v.achieved { color: var(--success); }
    .v.missed, .v.pending, .v.not_attempted { color: var(--text-secondary); }
    .v.pending { font-style: italic; }
    table.att { width: 100%; border-collapse: collapse; font-size: 12px; }
    table.att th { text-align: left; color: var(--text-muted); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--border); }
    table.att td { padding: 6px; border-bottom: 1px solid var(--border); vertical-align: top; }
    table.att tr.q td { border-bottom-style: dashed; }
    table.att code { font-size: 11.5px; word-break: break-all; color: var(--text-secondary); }
    .dr-actions { display: flex; gap: 8px; flex-wrap: wrap; padding: 12px 20px; border-top: 1px solid var(--border); background: var(--bg-secondary); }

    @media (max-width: 1100px) { .queue { grid-template-columns: 1fr 1fr; } }
    @container (max-width: 860px) { table.dense .c-obj, table.dense .c-score { display: none; } table.dense th { white-space: normal; } }
    @container (max-width: 480px) { table.dense .c-team, table.dense .c-last { display: none; } }
    @media (max-width: 600px) { .queue { grid-template-columns: 1fr; } }
  `],
})
export class LiveClassComponent implements OnInit, AfterViewInit {
  private api = inject(ApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);
  private destroyRef = inject(DestroyRef);
  private document = inject(DOCUMENT);
  private drawerPortal = viewChild.required(CdkPortal);

  readonly exerciseId = input.required<string>();
  /** The class as loaded, for the page header (the exercise's name, not its id). */
  readonly loaded = output<ExerciseClass>();

  readonly cls = signal<ExerciseClass | null>(null);
  readonly injects = signal<InjectRecord[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly now = signal(new Date());
  readonly selectedId = signal<string | null>(null);
  readonly resetting = signal<string | null>(null);
  readonly sortKey = signal<SortKey>('status');
  readonly sortDir = signal<'asc' | 'desc'>('asc');

  readonly laneMeta = LANES;
  readonly soon = F2_SOON;
  readonly columns: { key: SortKey; label: string; cls: string }[] = [
    { key: 'name', label: 'Student', cls: '' },
    { key: 'team', label: 'Team', cls: 'c-team' },
    { key: 'status', label: 'Status', cls: '' },
    { key: 'detections', label: 'Detections credited / attempted', cls: '' },
    { key: 'activity', label: 'Last activity', cls: 'c-last' },
  ];

  /** Resetting a Student's lab needs learning_record:write (routers/lab_sessions.py). */
  readonly canResetLab = () => this.auth.can('learning_record:write');

  readonly students = computed(() => this.cls()?.students ?? []);
  readonly lanes = computed(() => lanesOf(this.students()));
  readonly sorted = computed(() => sortStudents(this.students(), this.sortKey(), this.sortDir()));
  readonly selected = computed(() => this.students().find(s => s.user_id === this.selectedId()) ?? null);
  readonly teamCount = computed(() => new Set(this.students().filter(s => s.team).map(s => s.team!.id)).size);
  readonly labCount = computed(() => this.students().filter(s => s.lab).length);
  readonly labsUp = computed(() => this.students().filter(s => s.lab && RESETTABLE_LAB.includes(s.lab.state)).length);
  readonly creditedTotal = computed(() => this.students().reduce((n, s) => n + (s.credited_refs?.length ?? 0), 0));
  readonly rangeFault = computed(() => {
    const r = this.cls()?.range;
    return r && r.state === 'failed' ? r : null;
  });
  readonly axisEnd = computed(() => this.cls()?.completed_at ?? this.now().toISOString());

  private lastFocus: HTMLElement | null = null;
  private closeBtn = viewChild('closeBtn', { read: ElementRef<HTMLButtonElement> });

  ngOnInit(): void {
    this.refresh();
    const timer = setInterval(() => this.refresh(), REFRESH_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  ngAfterViewInit(): void {
    // Its own host at the end of <body>: dispose() removes the outlet element, so never body itself.
    const host = this.document.createElement('div');
    host.className = 'tn-live-class-drawer-host';
    this.document.body.appendChild(host);
    const outlet = new DomPortalOutlet(host);
    outlet.attach(this.drawerPortal());
    this.destroyRef.onDestroy(() => outlet.dispose());
  }

  refresh(): void {
    const id = this.exerciseId();
    if (!id) return;
    this.loading.set(true);
    this.api.getExerciseClass(id).subscribe({
      next: c => {
        this.cls.set(c);
        this.now.set(new Date(c.generated_at));
        this.error.set(null);
        this.loading.set(false);
        this.loaded.emit(c);
      },
      error: (e: unknown) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
    // Injects are optional context: a failure leaves the timeline empty, not the class.
    this.api.listInjects(id).subscribe({ next: r => this.injects.set(r ?? []), error: () => {} });
  }

  private describe(e: unknown): string {
    if (e instanceof HttpErrorResponse) {
      if (e.status === 403) return 'You do not have access to this class roster.';
      if (e.status === 404) return 'This exercise was not found.';
      if (e.status === 0) return 'The server could not be reached.';
      return `The class could not be loaded (HTTP ${e.status}).`;
    }
    return 'The class could not be loaded.';
  }

  sortBy(key: SortKey): void {
    if (this.sortKey() === key) this.sortDir.set(this.sortDir() === 'asc' ? 'desc' : 'asc');
    else { this.sortKey.set(key); this.sortDir.set('asc'); }
  }

  open(s: ClassStudent, ev?: Event): void {
    this.lastFocus = (ev?.currentTarget as HTMLElement) ?? (document.activeElement as HTMLElement | null);
    this.selectedId.set(s.user_id);
    setTimeout(() => this.closeBtn()?.nativeElement.focus());
  }

  close(): void {
    this.selectedId.set(null);
    const back = this.lastFocus;
    this.lastFocus = null;
    if (back && document.body.contains(back)) setTimeout(() => back.focus());
  }

  confirmReset(s: ClassStudent): void {
    const lab = s.lab;
    if (!lab || !this.resettable(lab.state)) return;
    this.dialog.open(ConfirmDialogComponent, {
      data: {
        title: `Reset ${s.display_name}'s lab?`,
        message: `The lab goes back to its starting point. Anything ${s.display_name} has not submitted is lost. `
          + 'Detections already judged are kept.',
        confirmText: 'Reset lab',
      },
    }).afterClosed().subscribe(ok => {
      if (!ok) return;
      this.resetting.set(lab.session_id);
      this.api.resetLabSession(lab.session_id).subscribe({
        next: r => {
          this.notify.success(`Reset requested; ${s.display_name}'s lab is ${r.state}.`);
          this.resetting.set(null);
          this.refresh();
        },
        error: (e: unknown) => {
          const detail = e instanceof HttpErrorResponse && typeof e.error?.detail === 'string' ? `: ${e.error.detail}` : '';
          this.notify.error(`The lab was not reset${detail}`);
          this.resetting.set(null);
        },
      });
    });
  }

  resettable(state: string): boolean {
    return RESETTABLE_LAB.includes(state);
  }

  statuses(s: ClassStudent): StatusKey[] { return statusesOf(s); }
  word(k: StatusKey): string { return STATUS_WORD[k]; }
  statusText(s: ClassStudent): string { return statusesOf(s).map(k => STATUS_WORD[k]).join(', '); }
  verdictWord(v: string): string { return VERDICT_WORD[v] ?? v; }
  objWord(v: string): string { return OBJECTIVE_STATE_WORD[v] ?? v; }
  agoText(iso: string | null | undefined): string { return ago(iso, this.now()); }
  tp(iso: string | null | undefined): string { return tplus(iso, this.cls()?.started_at); }
  attemptsNewestFirst(s: ClassStudent) { return [...(s.attempts ?? [])].reverse(); }

  objName(ref: string): string {
    const o = this.cls()?.objectives?.find(x => x.ref_id === ref);
    return objectiveName({ ref_id: ref, description: o?.description });
  }

  points(ref: string): number {
    return this.cls()?.objectives?.find(x => x.ref_id === ref)?.points ?? 0;
  }

  /** The server's sentence, with an objective's name in place of its ref. */
  detailText(a: { detail: string; objective_ref?: string | null }): string {
    const ref = a.objective_ref;
    return ref ? a.detail.split(ref).join(this.objName(ref)) : a.detail;
  }

  /** Exposed for specs. */
  laneItems(k: LaneKey) { return this.lanes()[k]; }
}
