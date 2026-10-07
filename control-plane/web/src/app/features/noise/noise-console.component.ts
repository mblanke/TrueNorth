import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { forkJoin, of, Subscription, timer } from 'rxjs';
import { catchError, switchMap } from 'rxjs/operators';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import {
  NoiseActivity,
  NoiseAgent,
  NoiseApiService,
  NoiseDeploy,
  NoisePersona,
  NoiseProfile,
  NoiseStats,
} from '@core/services/noise-api.service';

type Tab = 'live' | 'personas' | 'setup';
type Filter = 'all' | 'lookalikes';

/** The same words as the mockup (docs/mockups/noise-console.html) for a level. */
export function levelLabel(level: number): string {
  if (level <= 0) return 'silent';
  if (level <= 20) return 'quiet';
  if (level <= 55) return 'an ordinary office day';
  if (level <= 80) return 'busy';
  return 'chaos';
}

const PRESET_LABELS: Record<string, string> = { quiet: 'Quiet', office: 'Office day', busy: 'Busy', chaos: 'Chaos' };
/** Activity kinds of control-plane/api/app/noise/dial.py, in the white cell's words. */
const KIND_LABELS: Record<string, string> = {
  web_browse: 'Browsed the intranet',
  email_read: 'Read email',
  email_send: 'Sent email',
  dns_lookup: 'DNS lookup',
  file_share: 'File share',
  ad_logon: 'Logon',
  ssh_admin: 'Admin SSH',
  ntp_sync: 'Time sync',
  admin_scan: 'Inventory scan (lookalike)',
  admin_remote_exec: 'Remote admin command (lookalike)',
  bad_password: 'Failed logons (lookalike)',
  bulk_upload: 'Bulk upload (lookalike)',
};

/**
 * Background noise for one range: the white cell's dial, the agents, the personas, and
 * the ground truth of which activity was synthetic. Route `authoring/ranges/:id/noise`,
 * under the instructor-only Authoring hub; the API enforces NOISE_READ / NOISE_CONTROL
 * on every call as well. Students have no route here and no API access.
 */
@Component({
  selector: 'tn-noise-console',
  imports: [DatePipe, FormsModule, RouterLink, MatButtonModule],
  template: `
    <div class="page-container noise">
      <div class="page-header">
        <div>
          <a class="back" routerLink="/authoring/ranges">Ranges</a>
          <div class="tn-kicker">{{ rangeName() || 'Range' }}</div>
          <h1>Background noise</h1>
          <p class="subtitle">
            Synthetic staff going about their day, so the attack is not the only thing on the network.
            Only the white cell sees this page.
          </p>
        </div>
      </div>

      @if (loading()) {
        <p class="muted" role="status">Loading…</p>
      } @else if (loadError()) {
        <section class="panel" role="alert">
          <h2>Background noise is not available</h2>
          <p class="muted">{{ loadError() }}</p>
        </section>
      } @else if (profile(); as p) {
        @if (p.enabled) {
          <div class="tabs" role="tablist" aria-label="Background noise">
            @for (t of tabList; track t.key) {
              <button role="tab" type="button" [attr.aria-selected]="tab() === t.key" (click)="tab.set(t.key)">
                {{ t.label }}
              </button>
            }
          </div>
        }

        @if (p.enabled && tab() === 'live') {
          <section class="panel hero" data-testid="noise-hero">
            <div class="kicker">
              {{ p.paused ? 'Paused' : 'Running' }} · {{ personas().length }} personas on {{ agents().length }} machines
            </div>
            <h2>
              @if (p.paused) { All noise is paused. }
              @else { Noise level {{ p.level }} — {{ label(p.level) }} }
            </h2>
            <div class="dial">
              <label class="slider">
                <span class="muted small">0</span>
                <input type="range" min="0" max="100" [ngModel]="level()" (ngModelChange)="level.set(+$event)"
                       aria-label="Noise level" />
                <span class="muted small">100</span>
                <strong>{{ level() }}</strong>
              </label>
              @for (pr of presets(); track pr.key) {
                <button mat-stroked-button type="button" [attr.aria-pressed]="level() === pr.value"
                        (click)="level.set(pr.value)">{{ pr.label }}</button>
              }
            </div>
            <p class="muted small">
              At {{ p.effective_level }}: {{ pct(p.dial.active_fraction) }}% of staff at work ·
              about {{ p.dial.actions_per_hour }} actions per person per hour ·
              {{ p.dial.lookalike_share > 0 ? pct(p.dial.lookalike_share) + '% of eligible actions are lookalikes' : 'no lookalikes' }}.
            </p>
            <div class="actions">
              @if (level() !== p.level) {
                <button mat-flat-button color="primary" type="button" [disabled]="busy()" (click)="applyLevel()">
                  Set level to {{ level() }}
                </button>
              }
              <button mat-stroked-button type="button" [disabled]="busy()" (click)="togglePause()">
                {{ p.paused ? 'Resume noise' : 'Pause all noise' }}
              </button>
              <button mat-button type="button" [disabled]="busy()" (click)="stop()">Stop noise</button>
            </div>
          </section>

          <div class="grid">
            <div class="stack">
              <section class="panel">
                <h2>What the staff did · last hour</h2>
                @if (mix().length) {
                  @for (m of mix(); track m.kind) {
                    <div class="row">
                      <span>{{ m.label }}</span>
                      <span class="muted small">{{ m.share }}%</span>
                      <span class="meter" aria-hidden="true"><i [style.width.%]="m.share"></i></span>
                    </div>
                  }
                } @else {
                  <p class="muted small">Nothing reported in the last hour yet.</p>
                }
                <p class="note small">
                  <strong>Lookalikes</strong> are harmless actions that look like an attack: an IT admin's
                  inventory scan, someone mistyping a password. If a student reports one, the debrief marks it
                  as a false positive.
                </p>
              </section>

              <section class="panel">
                <h2>Ground truth</h2>
                <p class="muted small">Every action the personas took. Students never see it.</p>
                <select class="input" aria-label="Filter activity" [ngModel]="filter()" (ngModelChange)="setFilter($event)">
                  <option value="all">All activity</option>
                  <option value="lookalikes">Lookalikes only</option>
                </select>
                <table class="table" data-testid="ground-truth">
                  <thead><tr><th>Time</th><th>Machine</th><th>Who</th><th>What</th><th>Against</th><th></th></tr></thead>
                  <tbody>
                    @for (a of activity(); track $index) {
                      <tr>
                        <td>{{ a.at | date: 'HH:mm:ss' }}</td>
                        <td>{{ a.node }}</td>
                        <td>{{ a.persona }}</td>
                        <td>{{ kindLabel(a.kind) }}</td>
                        <td class="muted">{{ a.target }}</td>
                        <td>
                          @if (a.lookalike) { <span class="chip warm">Lookalike</span> }
                          @if (!a.ok) { <span class="chip">Failed</span> }
                        </td>
                      </tr>
                    } @empty {
                      <tr><td colspan="6" class="muted">No activity reported yet.</td></tr>
                    }
                  </tbody>
                </table>
                <p class="muted small">{{ stats()?.total ?? 0 }} actions in the last hour.</p>
              </section>
            </div>

            <div class="stack">
              <section class="panel">
                <h2>Agents</h2>
                @for (a of agents(); track a.id) {
                  <div class="row" data-testid="agent-row">
                    <div>
                      <strong>{{ a.node }}</strong>
                      <p class="muted small">{{ a.zone || 'no zone' }} · {{ seen(a) }}</p>
                    </div>
                    <span class="chip" [class.ok]="a.state === 'ok'" [class.warm]="a.state === 'lost'">
                      {{ stateLabel(a.state) }}
                    </span>
                  </div>
                } @empty {
                  <p class="muted small">No agents registered. Deploy from the Setup tab.</p>
                }
              </section>
            </div>
          </div>
        }

        @if (p.enabled && tab() === 'personas') {
          <section class="panel">
            <input class="input" type="search" placeholder="Search staff" aria-label="Search staff"
                   [ngModel]="search()" (ngModelChange)="search.set($event)" />
            <table class="table" data-testid="personas">
              <thead><tr><th>Name</th><th>Role</th><th>Department</th><th>Machine</th><th>Hours</th><th></th></tr></thead>
              <tbody>
                @for (pe of filteredPersonas(); track pe.id) {
                  <tr>
                    <td>{{ pe.display_name }}</td>
                    <td>{{ pe.title }}</td>
                    <td>{{ pe.department }}</td>
                    <td>{{ pe.node }}</td>
                    <td>{{ hours(pe) }}</td>
                    <td>@if (pe.lookalikes) { <span class="chip warm">Lookalikes</span> }</td>
                  </tr>
                } @empty {
                  <tr><td colspan="6" class="muted">No personas.</td></tr>
                }
              </tbody>
            </table>
          </section>
        }

        @if (!p.enabled || tab() === 'setup') {
          @if (!p.enabled) {
            <section class="panel hero" data-testid="noise-setup">
              <div class="kicker">{{ p.configured ? 'Stopped' : 'Not deployed' }}</div>
              <h2>{{ p.configured ? 'Background noise is stopped.' : 'This range is ready for background noise.' }}</h2>
              <p class="muted">
                Check what will happen below, then deploy. Agents install over the management network;
                students cannot see it.
              </p>
              <div class="actions">
                <button mat-flat-button color="primary" type="button" [disabled]="busy() || !preview()" (click)="deploy()">
                  Deploy noise
                </button>
                @if (p.configured) {
                  <button mat-stroked-button type="button" [disabled]="busy()" (click)="start()">
                    Start without re-deploying
                  </button>
                }
              </div>
            </section>
          }
          @if (preview(); as d) {
            <div class="grid">
              <section class="panel">
                <h2>Agents</h2>
                @for (a of d.agents; track a.node) {
                  <div class="row">
                    <div><strong>{{ a.node }}</strong><p class="muted small">{{ a.zone }} · {{ a.ip }}</p></div>
                    <span class="muted small">mgmt {{ a.mgmt_ip || 'not reserved yet' }}</span>
                  </div>
                } @empty {
                  <p class="muted small">The template has no Linux agent nodes.</p>
                }
                @if (d.skipped.length) {
                  <p class="muted small">Skipped: {{ skippedText(d) }}</p>
                }
              </section>
              <section class="panel">
                <h2>From the template</h2>
                <dl class="kv">
                  <dt>Management</dt><dd>{{ d.mgmt_cidr }}</dd>
                  <dt>Controller</dt><dd>{{ d.controller_url || 'not set' }}</dd>
                  <dt>Targets</dt><dd>{{ targetsText(d) }}</dd>
                </dl>
              </section>
            </div>
          } @else if (previewError()) {
            <section class="panel" role="alert"><p class="muted">{{ previewError() }}</p></section>
          }
        }
      }
    </div>
  `,
  styles: [`
    .noise { max-width: 1200px; }
    .back { font-size: 13px; color: var(--accent); text-decoration: none; }
    .back::before { content: '← '; }
    .panel { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }
    .panel + .panel, .grid + .panel, .panel + .grid { margin-top: 15px; }
    .panel h2 { font-size: 17px; font-weight: 600; margin: 0 0 12px; }
    .hero h2 { font-size: 22px; margin: 6px 0 10px; }
    .kicker { font-size: 12px; color: var(--text-muted); }
    .muted { color: var(--text-muted); }
    .small { font-size: 12px; }
    .tabs { display: flex; gap: 4px; margin-bottom: 15px; }
    .tabs button { border: 0; background: transparent; padding: 7px 11px; border-radius: 6px; cursor: pointer; color: var(--text-muted); }
    .tabs button[aria-selected='true'] { color: var(--accent); background: var(--accent-muted); }
    .dial, .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; }
    .slider { display: flex; align-items: center; gap: 10px; flex: 1 1 260px; max-width: 420px; }
    .slider input { flex: 1; accent-color: var(--accent); }
    button[aria-pressed='true'] { border-color: var(--accent); color: var(--accent); background: var(--accent-muted); }
    .grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 15px; margin-top: 15px; }
    .stack { display: flex; flex-direction: column; gap: 15px; }
    .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 9px 0; border-top: 1px solid var(--border); }
    .row:first-of-type { border-top: 0; }
    .row p { margin: 3px 0 0; }
    .meter { width: 120px; height: 6px; background: var(--bg-secondary); border-radius: 3px; overflow: hidden; }
    .meter i { display: block; height: 100%; background: var(--accent); }
    .note { background: var(--accent-muted); border-radius: 6px; padding: 10px 12px; margin: 12px 0 0; }
    .input { border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; margin-bottom: 10px; min-width: 220px; }
    .table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .table th { text-align: left; color: var(--text-muted); font-weight: 500; font-size: 12px; }
    .table th, .table td { padding: 9px 8px; border-bottom: 1px solid var(--border); }
    .chip { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--bg-secondary); margin-left: 4px; }
    .chip.ok { color: var(--success, #1f6b45); background: #e6f3ec; }
    .chip.warm { color: var(--accent-hover); background: var(--accent-muted); }
    .kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; margin: 0; font-size: 13px; }
    .kv dt { color: var(--text-muted); }
    .kv dd { margin: 0; }
    @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
  `],
})
export class NoiseConsoleComponent implements OnInit, OnDestroy {
  private readonly noise = inject(NoiseApiService);
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly route = inject(ActivatedRoute);
  private poll?: Subscription;

  readonly tabList: { key: Tab; label: string }[] = [
    { key: 'live', label: 'Live' },
    { key: 'personas', label: 'Personas' },
    { key: 'setup', label: 'Setup' },
  ];

  rangeId = '';
  readonly rangeName = signal('');
  readonly loading = signal(true);
  readonly loadError = signal('');
  readonly busy = signal(false);
  readonly tab = signal<Tab>('live');
  readonly profile = signal<NoiseProfile | null>(null);
  readonly level = signal(40);
  readonly presets = signal<{ key: string; label: string; value: number }[]>([]);
  readonly agents = signal<NoiseAgent[]>([]);
  readonly personas = signal<NoisePersona[]>([]);
  readonly activity = signal<NoiseActivity[]>([]);
  readonly stats = signal<NoiseStats | null>(null);
  readonly preview = signal<NoiseDeploy | null>(null);
  readonly previewError = signal('');
  readonly filter = signal<Filter>('all');
  readonly search = signal('');

  readonly mix = computed(() => {
    const s = this.stats();
    if (!s || !s.total) return [];
    return Object.entries(s.by_kind)
      .map(([kind, n]) => ({ kind, label: this.kindLabel(kind), share: Math.round((100 * n) / s.total) }))
      .sort((a, b) => b.share - a.share);
  });

  readonly filteredPersonas = computed(() => {
    const q = this.search().trim().toLowerCase();
    if (!q) return this.personas();
    return this.personas().filter(p =>
      [p.display_name, p.title, p.department, p.node, p.handle].some(v => (v ?? '').toLowerCase().includes(q)),
    );
  });

  ngOnInit(): void {
    this.rangeId = this.route.snapshot.paramMap.get('id') ?? '';
    this.api.getRange(this.rangeId).pipe(catchError(() => of(null))).subscribe(r => this.rangeName.set(r?.name ?? ''));
    this.noise.presets().pipe(catchError(() => of(null))).subscribe(p => {
      if (!p) return;
      this.presets.set(
        Object.entries(p.presets).map(([key, value]) => ({ key, value, label: PRESET_LABELS[key] ?? key })),
      );
    });
    this.noise.profile(this.rangeId).subscribe({
      next: p => {
        this.setProfile(p);
        this.loading.set(false);
        if (p.enabled) this.startPolling();
        else this.loadPreview();
      },
      error: (e: HttpErrorResponse) => {
        this.loading.set(false);
        this.loadError.set(e.status === 404 ? 'Range not found.' : e.status === 403
          ? 'You do not have access to background noise.' : this.detail(e));
      },
    });
  }

  ngOnDestroy(): void {
    this.poll?.unsubscribe();
  }

  label = levelLabel;

  kindLabel(kind: string): string {
    return KIND_LABELS[kind] ?? kind.replace(/_/g, ' ');
  }

  pct(fraction: number): number {
    return Math.round(fraction * 100);
  }

  stateLabel(state: string): string {
    return state === 'ok' ? 'Reporting' : state === 'lost' ? 'Not reporting' : 'Waiting for first report';
  }

  seen(a: NoiseAgent): string {
    return a.last_seen_at ? `last seen ${new Date(a.last_seen_at).toLocaleTimeString()}` : 'not seen yet';
  }

  hours(p: NoisePersona): string {
    const h = (n: number | null | undefined) => `${String(n ?? 0).padStart(2, '0')}:00`;
    return `${h(p.work_start)}–${h(p.work_end)}`;
  }

  skippedText(d: NoiseDeploy): string {
    return d.skipped.map(s => `${s.node} (${s.reason})`).join(', ');
  }

  targetsText(d: NoiseDeploy): string {
    const pools = Object.entries(d.targets).map(([k, v]) => `${k}: ${v.length}`);
    return pools.length ? pools.join(' · ') : 'none found in the template';
  }

  setFilter(f: Filter): void {
    this.filter.set(f);
    this.refreshActivity();
  }

  applyLevel(): void {
    this.save({ level: this.level() }, `Level set to ${this.level()}. Agents pick it up within a minute.`);
  }

  togglePause(): void {
    const paused = !this.profile()?.paused;
    this.save({ paused }, paused ? 'All noise paused.' : 'Noise resumed.');
  }

  /** Stop: the agents get empty plans until it is started again; ground truth is kept. */
  stop(): void {
    this.save({ enabled: false }, 'Background noise stopped.', () => {
      this.poll?.unsubscribe();
      this.loadPreview();
    });
  }

  start(): void {
    this.save({ enabled: true, paused: false }, 'Background noise started.', () => {
      this.tab.set('live');
      this.startPolling();
    });
  }

  deploy(): void {
    this.busy.set(true);
    this.noise.deploy(this.rangeId, false).subscribe({
      next: d => {
        this.busy.set(false);
        this.notify.success(`Deploying to ${d.agents.length} machines. Agents report in within a few minutes.`);
        this.noise.profile(this.rangeId).subscribe(p => {
          this.setProfile(p);
          this.tab.set('live');
          this.startPolling();
        });
      },
      error: (e: HttpErrorResponse) => {
        this.busy.set(false);
        this.notify.error(this.detail(e));
      },
    });
  }

  private save(body: Parameters<NoiseApiService['update']>[1], done: string, then?: () => void): void {
    this.busy.set(true);
    this.noise.update(this.rangeId, body).subscribe({
      next: p => {
        this.busy.set(false);
        this.setProfile(p);
        this.notify.success(done);
        then?.();
      },
      error: (e: HttpErrorResponse) => {
        this.busy.set(false);
        this.notify.error(this.detail(e));
      },
    });
  }

  private setProfile(p: NoiseProfile): void {
    this.profile.set(p);
    this.level.set(p.level);
  }

  private loadPreview(): void {
    this.previewError.set('');
    this.noise.deploy(this.rangeId, true).subscribe({
      next: d => this.preview.set(d),
      error: (e: HttpErrorResponse) => {
        this.preview.set(null);
        this.previewError.set(this.detail(e));
      },
    });
  }

  /** Agents, personas, stats and ground truth, now and every 15 seconds while live. */
  private startPolling(): void {
    this.poll?.unsubscribe();
    this.poll = timer(0, 15000)
      .pipe(
        switchMap(() =>
          forkJoin({
            agents: this.noise.agents(this.rangeId),
            personas: this.noise.personas(this.rangeId),
            stats: this.noise.stats(this.rangeId),
            activity: this.activityRequest(),
          }).pipe(catchError(() => of(null))),
        ),
      )
      .subscribe(r => {
        if (!r) return;
        this.agents.set(r.agents);
        this.personas.set(r.personas);
        this.stats.set(r.stats);
        this.activity.set(r.activity);
      });
    this.loadPreview();
  }

  private refreshActivity(): void {
    this.activityRequest().pipe(catchError(() => of([]))).subscribe(a => this.activity.set(a));
  }

  private activityRequest() {
    return this.noise.activity(this.rangeId, {
      limit: 100,
      lookalike: this.filter() === 'lookalikes' ? true : undefined,
    });
  }

  private detail(e: HttpErrorResponse): string {
    const d = e.error?.detail;
    if (typeof d === 'string') return d;
    return e.message || 'The request failed.';
  }
}
