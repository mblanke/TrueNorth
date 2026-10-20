import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { ApiService, Detection } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import type { InjectRecord } from '@core/models';
import { objectiveKind, objectiveName } from '@shared/objective-display';
import { DetectionPanelComponent, isDetectionObjective } from './detection-panel.component';
import { RangeNotesComponent } from '../../shared/components/range-notes/range-notes.component';
import { RangeCredentialsComponent } from '../../shared/components/range-credentials/range-credentials.component';
import { RangeTopologyComponent } from '../../shared/components/range-topology/range-topology.component';

interface TimelineStep {
  t: string;
  action: string;
  attack_technique?: string;
  critical_event?: string;
  description?: string;
}
interface ObjectiveRow {
  ref_id: string;
  /** The objective's name (empty when the scenario gave none). */
  description?: string;
  type: string;
  points: number;
  achieved: boolean;
  evidence: string;
  validator?: string | null;
  competency_code: string;
}
interface ScenarioDetail {
  exercise_id: string;
  exercise_name: string;
  state: string;
  total_score: number;
  max_score: number;
  range_id: string;
  scenario_name: string;
  po_id: string;
  environment: string;
  duration_min: number;
  timeline: TimelineStep[];
  noise_floor: { id: string; description: string }[] | null; // null when the YAML key is present but empty
  objectives: ObjectiveRow[];
}

@Component({
  selector: 'tn-exercise-detail',
  imports: [
    RouterLink,
    MatCardModule,
    MatChipsModule,
    MatIconModule,
    MatButtonModule,
    MatProgressBarModule,
    MatSnackBarModule,
    RangeNotesComponent,
    RangeCredentialsComponent,
    RangeTopologyComponent,
    DetectionPanelComponent,
],
  template: `
    @if (detail; as d) {
      <div class="xd page-container">
        <header class="xd-head">
          <div>
            <!-- A Student reaches an assessment from their career path; staff from Exercises (#201). -->
            @if (isStudent()) {
              <a routerLink="/learning/career-path" class="back"><mat-icon>arrow_back</mat-icon> Career path</a>
            } @else {
              <a routerLink="/exercises" class="back"><mat-icon>arrow_back</mat-icon> Exercises</a>
            }
            <div class="kicker">Individual Assessment</div>
            <h2>{{ d.exercise_name }}</h2>
            <div class="meta">
              @if (d.state === 'running') {
                <span class="chip run live" data-testid="live" role="status"><span class="dot" aria-hidden="true"></span>Live</span>
              } @else {
                <span class="chip" [class.done]="d.state==='completed'">{{ d.state }}</span>
              }
              @if (metaLine(d); as m) {
                <span class="muted">{{ m }}</span>
              }
              @if (d.duration_min >= 480) {
                <span class="chip warn"
                title="Unusually long assessment (8h+) — verify the duration is intentional">⚠ long</span>
              }
            </div>
          </div>
          <div class="run-box">
            @if (auth.isInstructor() && d.state !== 'running' && d.state !== 'paused') {
              <!-- Staff run and replay a team exercise; a replay clears every objective (sweep M3).
                   A running exercise shows Live instead of a disabled button (#201). -->
              <button mat-flat-button color="primary" (click)="run()" [disabled]="running">
                <mat-icon>{{ d.state === 'completed' ? 'replay' : 'play_arrow' }}</mat-icon>
                {{ running ? 'Starting…' : 'Provision & Run (simulated)' }}
              </button>
            }
            @if ((d.state === 'running' || d.state === 'paused') && auth.canOpen('opsCenter')) {
              <a mat-stroked-button [routerLink]="['/ops-center', d.exercise_id]">
                <mat-icon>radar</mat-icon> Open in Ops Center
              </a>
            }
            @if (d.state === 'paused' && auth.can('exercise:pause')) {
              <!-- POST /exercises/{id}/resume: the run carries on from its next unfired inject. -->
              <button mat-flat-button color="primary" (click)="resume()" [disabled]="resuming">
                <mat-icon>play_arrow</mat-icon> {{ resuming ? 'Resuming…' : 'Resume' }}
              </button>
            }
            @if (d.state === 'completed' && auth.can('aar:generate')) {
              <button mat-stroked-button (click)="genAar()"><mat-icon>description</mat-icon> Generate AAR</button>
            }
            <!-- /scoring needs aar:read (pageGuard): Students see the report of an exercise they
                 took part in (the API answers 404 otherwise), observers read it. -->
            @if (d.state === 'completed' && auth.canOpen('scoring')) {
              <a mat-stroked-button [routerLink]="['/scoring']" [queryParams]="{ exercise: d.exercise_id }">
                <mat-icon>visibility</mat-icon> View AAR
              </a>
            }
          </div>
        </header>
        <div class="score">
          <div class="score-line">
            <span>Score</span><strong>{{ d.total_score }} / {{ d.max_score || 100 }}</strong>
          </div>
          <mat-progress-bar mode="determinate" [value]="d.max_score ? (d.total_score / d.max_score) * 100 : 0" />
        </div>
        <div class="grid">
          <!-- Objectives -->
          <mat-card class="panel">
            <h3><mat-icon>flag</mat-icon> Objectives</h3>
            @for (o of d.objectives; track o.ref_id) {
              <div class="obj" [class.ok]="o.achieved">
                <mat-icon>{{ o.achieved ? 'check_circle' : 'radio_button_unchecked' }}</mat-icon>
                <span class="obj-ref" [attr.title]="o.ref_id">{{ nameOf(o) }}</span>
                <span class="obj-type">{{ kindOf(o) }}</span>
                <span class="spacer"></span>
                <span class="pts">{{ o.points }} pts</span>
              </div>
              @if (isDetection(o)) {
                <tn-detection-panel [exerciseId]="id" [refId]="o.ref_id" [achieved]="o.achieved"
                  [running]="d.state === 'running'" [canSubmit]="auth.canSubmitDetections()"
                  [staff]="auth.isInstructor()" [attempts]="attemptsFor(o.ref_id)" (changed)="refresh()" />
              }
            }
          </mat-card>
          <!-- Timeline -->
          <mat-card class="panel">
            <h3><mat-icon>timeline</mat-icon> Attack Timeline</h3>
            @if (d.timeline.length) {
              <div class="tl">
                @for (s of d.timeline; track s.t) {
                  <div class="tl-step">
                    <span class="tl-t">{{ s.t }}</span>
                    @if (s.attack_technique) {
                      <span class="tl-tech">{{ s.attack_technique }}</span>
                    }
                    <div class="tl-body">
                      <div class="tl-ce">{{ s.critical_event || s.action }}</div>
                      <div class="muted small">{{ s.description }}</div>
                    </div>
                  </div>
                }
              </div>
            } @else {
              <p class="muted">No timeline defined.</p>
            }
            @if (d.noise_floor?.length) {
              <div class="nf">
                <div class="nf-h">Noise floor (benign lookalikes)</div>
                @for (n of d.noise_floor; track n.id) {
                  <div class="muted small">• {{ n.description }}</div>
                }
              </div>
            }
          </mat-card>
          <!-- Inject log: what each inject actually did (GET /exercises/{id}/injects) -->
          <mat-card class="panel inject-panel">
            <h3><mat-icon>bolt</mat-icon> Inject log</h3>
            @if (injects.length) {
              @for (i of injects; track i.id) {
                <div class="inj" [attr.data-status]="i.status">
                  <span class="tl-t">{{ i.t || (i.source === 'instructor' ? 'live' : '') }}</span>
                  <span class="inj-status chip">{{ i.status }}</span>
                  <span class="inj-action">{{ i.action }}</span>
                  @if (i.execution_mode === 'simulated') {
                    <span class="muted small" title="Synthetic records: nothing ran on a host">simulated</span>
                  }
                  <span class="spacer"></span>
                  <span class="muted small inj-detail">{{ i.detail }}</span>
                </div>
              }
            } @else {
              <p class="muted">No injects recorded yet.</p>
            }
          </mat-card>
          <!-- Range -->
          <mat-card class="panel range-panel">
            <h3><mat-icon>dns</mat-icon> Range Topology</h3>
            <p class="muted">{{ rangeName || 'Assessment range' }} @if (rangeState) {
              <span>· {{ rangeState }}</span>
            } · simulated (mock)</p>
            @if (d.range_id) {
              <tn-range-topology [rangeId]="d.range_id" />
            }
            @if (d.range_id) {
              <div class="range-notes">
                <tn-range-notes [rangeId]="d.range_id" [description]="rangeDescription" [readOnly]="true" />
              </div>
              <!-- A Student sees only the accounts handed to them; staff see every one (ADR 0012) -->
              <div class="range-notes">
                <tn-range-credentials [rangeId]="d.range_id" />
              </div>
            }
            <div class="range-btns">
              <a mat-stroked-button [routerLink]="['/topology-3d']" [queryParams]="{ range: d.range_id }">
                <mat-icon>3d_rotation</mat-icon> Open 3D
              </a>
              @if (auth.isInstructor()) {
                <a mat-stroked-button [routerLink]="['/authoring/ranges/designer']" [queryParams]="{ range: d.range_id }">
                  <mat-icon>edit</mat-icon> Open in designer
                </a>
              }
            </div>
          </mat-card>
        </div>
      </div>
    }
    @if (!detail && !error) {
      <div class="tn-skeleton-group loading" aria-busy="true">
        <div class="tn-skeleton tn-skeleton-text" style="width: 220px"></div>
        <div class="tn-skeleton tn-skeleton-card"></div>
        <div class="tn-skeleton tn-skeleton-card"></div>
      </div>
    }
    @if (error) {
      <div class="error-panel">
        <mat-icon>cloud_off</mat-icon>
        <p>{{ error }}</p>
        <button mat-stroked-button (click)="retry()"><mat-icon>refresh</mat-icon> Retry</button>
      </div>
    }
    `,
  styles: [
    `
      .chip.live { display: inline-flex; align-items: center; gap: 6px; font-weight: 700; }
      .chip.live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warning); animation: live-pulse 1.6s ease-in-out infinite; }
      @keyframes live-pulse { 50% { opacity: .35; } }
      @media (prefers-reduced-motion: reduce) { .chip.live .dot { animation: none; } }
      .xd-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
      .back { display: inline-flex; align-items: center; gap: 4px; font-size: 0.85rem; color: var(--text-muted); text-decoration: none; }
      .kicker { font-size: 0.72rem; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); font-weight: 700; margin-top: 6px; }
      .xd-head h2 { margin: 2px 0 4px; }
      .meta { display: flex; align-items: center; gap: 10px; }
      .muted { color: var(--text-muted); }
      .small { font-size: 0.8rem; }
      .chip { text-transform: uppercase; font-size: 0.7rem; letter-spacing: .04em; padding: 2px 10px; border-radius: 10px; background: color-mix(in srgb, var(--text-muted) 18%, transparent); }
      .chip.run { background: color-mix(in srgb, var(--warning) 22%, transparent); }
      .chip.done { background: color-mix(in srgb, var(--success) 22%, transparent); }
      .chip.warn { background: color-mix(in srgb, var(--warning) 22%, transparent); color: var(--warning); font-weight: 700; }
      .run-box { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .score { margin: 16px 0; }
      .score-line { display: flex; justify-content: space-between; margin-bottom: 4px; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
      .grid .panel:first-child { grid-row: span 2; }
      .range-panel { grid-column: 1 / -1; }
      .panel { padding: 14px 16px; }
      .panel h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 10px; font-size: 1rem; }
      .obj { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid color-mix(in srgb, var(--text-muted) 12%, transparent); }
      .obj mat-icon { color: var(--text-muted); }
      .obj.ok mat-icon { color: var(--success); }
      .obj-ref { font-weight: 600; }
      .obj-type { font-size: 0.78rem; color: var(--text-muted); }
      .spacer { flex: 1 1 auto; }
      .pts { font-variant-numeric: tabular-nums; }
      .tl-step { display: flex; gap: 10px; padding: 6px 0; border-left: 2px solid color-mix(in srgb, var(--accent) 40%, transparent); padding-left: 10px; margin-left: 4px; }
      .tl-t { font-variant-numeric: tabular-nums; color: var(--text-muted); min-width: 34px; }
      .tl-tech { font-weight: 600; color: var(--accent); min-width: 52px; }
      .tl-ce { font-weight: 500; }
      .nf { margin-top: 12px; padding-top: 8px; border-top: 1px dashed color-mix(in srgb, var(--text-muted) 25%, transparent); }
      .nf-h { font-size: 0.8rem; font-weight: 600; margin-bottom: 4px; }
      /* Theme-driven canvas: this used to be a hardcoded dark slab that sat as a
         black rectangle in the middle of the light theme. Node accent strips keep
         their categorical colours; the surfaces follow the theme. */
      .loading { padding: 16px; }
      .error-panel { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 40px 16px; color: var(--text-secondary); }
      .error-panel mat-icon { font-size: 42px; width: 42px; height: 42px; color: var(--text-muted); }
      .range-btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
      .range-notes { margin: 12px 0; padding-top: 12px; border-top: 1px solid var(--border); }
      .inject-panel { grid-column: 1 / -1; }
      .inj { display: flex; align-items: center; gap: 8px; padding: 5px 0; border-bottom: 1px solid color-mix(in srgb, var(--text-muted) 12%, transparent); }
      .inj-action { font-weight: 600; }
      .inj-detail { max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .inj[data-status='fired'] .inj-status { background: color-mix(in srgb, var(--success) 22%, transparent); }
      .inj[data-status='failed'] .inj-status { background: color-mix(in srgb, var(--error, #e53935) 22%, transparent); }
      @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .grid .panel:first-child { grid-row: auto; } }
    `,
  ],
})
export class ExerciseDetailComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private snack = inject(MatSnackBar);
  protected auth = inject(AuthService);

  id = '';
  detail: ScenarioDetail | null = null;
  rangeName = '';
  rangeState = '';
  rangeDescription = '';
  injects: InjectRecord[] = [];
  running = false;
  resuming = false;
  error = '';
  /** Attempts grouped by objective ref; stable arrays so bindings do not churn. */
  private attemptsByRef = new Map<string, Detection[]>();
  private readonly noAttempts: Detection[] = [];
  private poll: ReturnType<typeof setInterval> | null = null;

  isDetection(o: ObjectiveRow): boolean {
    return isDetectionObjective(o.validator);
  }

  readonly isStudent = () => this.auth.user()?.role === 'student';
  nameOf(o: ObjectiveRow): string {
    return objectiveName(o);
  }
  kindOf(o: ObjectiveRow): string {
    return objectiveKind(o);
  }

  /** Environment, duration and PO, leaving out whichever the scenario does not give. */
  metaLine(d: ScenarioDetail): string {
    return [d.environment, d.duration_min ? this.fmtDuration(d.duration_min) : '', d.po_id]
      .map(s => (s ?? '').trim()).filter(Boolean).join(' · ');
  }

  attemptsFor(refId: string): Detection[] {
    return this.attemptsByRef.get(refId) ?? this.noAttempts;
  }

  /** After a detection outcome: reload objectives, score and attempts from the server. */
  refresh(): void {
    this.api.get<ScenarioDetail>(`/exercises/${this.id}/scenario-detail`).subscribe({
      next: d => (this.detail = d),
      error: () => {},
    });
    this.loadDetections();
  }

  private loadDetections(): void {
    this.api.listDetections(this.id).subscribe({
      next: rows => {
        const byRef = new Map<string, Detection[]>();
        for (const r of rows) byRef.set(r.objective_ref, [...(byRef.get(r.objective_ref) ?? []), r]);
        this.attemptsByRef = byRef;
      },
      // Leave the last known list rather than claiming there were no attempts.
      error: () => {},
    });
  }

  fmtDuration(min: number): string {
    if (!min) return '—';
    if (min < 60) return `${min} min`;
    const h = min / 60;
    if (min % 60 === 0) {
      if (min >= 1440 && min % 1440 === 0) {
        const d = min / 1440;
        return `${d} day${d > 1 ? 's' : ''} (${h} h)`;
      }
      return `${h} h`;
    }
    return `${Math.floor(min / 60)}h ${min % 60}m`;
  }

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
    this.load();
    this.loadDetections();
  }

  protected retry(): void {
    this.error = '';
    this.load();
  }
  ngOnDestroy(): void {
    if (this.poll) clearInterval(this.poll);
  }

  private load(): void {
    this.api.get<ScenarioDetail>(`/exercises/${this.id}/scenario-detail`).subscribe({
      next: d => {
        this.detail = d;
        if (d.range_id) {
          this.api.getRange(d.range_id).subscribe({
            next: r => {
              this.rangeName = r.name;
              this.rangeState = r.state;
              this.rangeDescription = r.description || '';
            },
            error: () => {},
          });
        }
        if (d.state === 'running' && !this.poll) this.startPolling();
        this.loadInjects();
      },
      error: () => (this.error = 'Exercise not found.'),
    });
  }

  private loadInjects(): void {
    this.api.listInjects(this.id).subscribe({
      next: rows => (this.injects = rows),
      error: () => {},
    });
  }

  run(): void {
    const replay = this.detail?.state === 'completed' || this.detail?.state === 'cancelled';
    if (replay && !confirm('Replay this exercise? Every objective and the score are cleared.')) {
      return;
    }
    this.running = true;
    this.api.runExercise(this.id, replay).subscribe({
      next: () => {
        this.snack.open('Simulated run started — provisioning + executing timeline…', '', { duration: 3000 });
        this.startPolling();
      },
      error: err => {
        this.running = false;
        this.snack.open(err?.error?.detail || 'Could not start run', '', { duration: 4000 });
      },
    });
  }

  private startPolling(): void {
    if (this.poll) clearInterval(this.poll);
    this.poll = setInterval(() => {
      this.api.get<ScenarioDetail>(`/exercises/${this.id}/scenario-detail`).subscribe({
        next: d => {
          this.detail = d;
          this.loadInjects();
          if (d.state === 'completed' || d.state === 'cancelled') {
            this.running = false;
            if (this.poll) { clearInterval(this.poll); this.poll = null; }
            if (d.state === 'completed') {
              this.snack.open(`Run complete — score ${d.total_score}/${d.max_score}`, '', { duration: 4000 });
            }
          }
        },
        error: () => {},
      });
    }, 2000);
  }

  /** Resume a paused run; one request at a time, the state comes from the reload. */
  resume(): void {
    if (this.resuming) return;
    this.resuming = true;
    this.api.resumeExercise(this.id).subscribe({
      next: () => {
        this.resuming = false;
        this.snack.open('Exercise resumed', '', { duration: 3000 });
        this.load();
      },
      error: err => {
        this.resuming = false;
        this.snack.open(err?.error?.detail || 'Could not resume', '', { duration: 4000 });
        this.load();
      },
    });
  }

  genAar(): void {
    this.api.generateAAR(this.id).subscribe({
      next: () => this.snack.open('After-Action Report generated', '', { duration: 3000 }),
      error: () => this.snack.open('AAR generation failed', '', { duration: 3000 }),
    });
  }
}
