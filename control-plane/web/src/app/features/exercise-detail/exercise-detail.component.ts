import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { ApiService } from '@core/services/api.service';

interface TimelineStep {
  t: string;
  action: string;
  attack_technique?: string;
  critical_event?: string;
  description?: string;
}
interface ObjectiveRow {
  ref_id: string;
  type: string;
  points: number;
  achieved: boolean;
  evidence: string;
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
  noise_floor: { id: string; description: string }[];
  objectives: ObjectiveRow[];
}

interface SvgZone { x: number; y: number; w: number; h: number; label: string; cidr: string; color: string; }
interface SvgNode { x: number; y: number; label: string; ip: string; type: string; color: string; }

const NODE_COLORS: Record<string, string> = {
  workstation: '#42A5F5', server: '#66BB6A', dc: '#AB47BC', kali: '#EF5350',
  switch: '#FFA726', router: '#26C6DA', cloud: '#78909C',
  firewall: '#FF7043', seconion: '#5C6BC0', subnet: '#29B6F6', dmz: '#FFCA28',
};

const NODE_ICON: Record<string, string> = {
  workstation: '🖥️', server: '🗄️', dc: '🏛️', kali: '🐉', firewall: '🛡️',
  seconion: '🧅', router: '🌐', switch: '🔀', cloud: '☁️',
};

@Component({
  selector: 'tn-exercise-detail',
  standalone: true,
  imports: [
    CommonModule, RouterLink, MatCardModule, MatChipsModule, MatIconModule,
    MatButtonModule, MatProgressBarModule, MatSnackBarModule,
  ],
  template: `
    <div class="xd" *ngIf="detail as d">
      <header class="xd-head">
        <div>
          <a routerLink="/learning/qualifications" class="back"><mat-icon>arrow_back</mat-icon> Qualifications</a>
          <h2>{{ d.exercise_name }}</h2>
          <div class="meta">
            <span class="chip" [class.done]="d.state==='completed'" [class.run]="d.state==='running'">{{ d.state }}</span>
            <span class="muted">{{ d.environment }} · {{ fmtDuration(d.duration_min) }} · {{ d.po_id }}</span>
            <span class="chip warn" *ngIf="d.duration_min >= 480"
                  title="Unusually long assessment (8h+) — verify the duration is intentional">⚠ long</span>
          </div>
        </div>
        <div class="run-box">
          <button mat-flat-button color="primary" (click)="run()" [disabled]="running || d.state==='running'">
            <mat-icon>{{ d.state === 'completed' ? 'replay' : 'play_arrow' }}</mat-icon>
            {{ d.state === 'running' ? 'Running…' : 'Provision & Run (simulated)' }}
          </button>
          @if (d.state === 'completed') {
            <button mat-stroked-button (click)="genAar()"><mat-icon>description</mat-icon> Generate AAR</button>
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
              <span class="obj-ref">{{ o.ref_id }}</span>
              <span class="obj-type">{{ o.type }}</span>
              <span class="spacer"></span>
              <span class="pts">{{ o.points }} pts</span>
            </div>
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
                  <span class="tl-tech" *ngIf="s.attack_technique">{{ s.attack_technique }}</span>
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

        <!-- Range -->
        <mat-card class="panel range-panel">
          <h3><mat-icon>dns</mat-icon> Range Topology</h3>
          <p class="muted">{{ rangeName || 'Assessment range' }} <span *ngIf="rangeState">· {{ rangeState }}</span> · simulated (mock)</p>

          @if (svgNodes.length) {
            <div class="topo">
              <svg [attr.viewBox]="viewBox" preserveAspectRatio="xMidYMid meet" class="topo-svg">
                @for (z of svgZones; track z.label) {
                  <rect [attr.x]="z.x" [attr.y]="z.y" [attr.width]="z.w" [attr.height]="z.h" rx="12"
                        [attr.fill]="z.color + '12'" [attr.stroke]="z.color" stroke-width="1.5" stroke-dasharray="9 6" />
                  <text [attr.x]="z.x + 14" [attr.y]="z.y + 25" [attr.fill]="z.color" font-size="15" font-weight="700">{{ z.label }}</text>
                  <text [attr.x]="z.x + 14" [attr.y]="z.y + 43" [attr.fill]="z.color" font-size="12" opacity="0.8">{{ z.cidr }}</text>
                }
                @for (n of svgNodes; track n.label) {
                  <rect [attr.x]="n.x" [attr.y]="n.y" width="150" height="54" rx="9" fill="#0d1a2b" [attr.stroke]="n.color" stroke-width="1.5" />
                  <rect [attr.x]="n.x" [attr.y]="n.y" width="6" height="54" rx="3" [attr.fill]="n.color" />
                  <text [attr.x]="n.x + 16" [attr.y]="n.y + 22" fill="#e7eef7" font-size="13" font-weight="600">{{ iconFor(n.type) }} {{ n.label }}</text>
                  <text [attr.x]="n.x + 16" [attr.y]="n.y + 40" fill="#93a0b4" font-size="11">{{ n.ip }} · {{ n.type }}</text>
                }
              </svg>
            </div>
          }

          <div class="range-btns">
            <a mat-stroked-button [routerLink]="['/topology-3d']" [queryParams]="{ range: d.range_id }">
              <mat-icon>3d_rotation</mat-icon> Open 3D
            </a>
            <a mat-stroked-button [routerLink]="['/authoring/ranges']" [queryParams]="{ range: d.range_id }">
              <mat-icon>edit</mat-icon> Open in designer
            </a>
          </div>
        </mat-card>
      </div>
    </div>
    @if (!detail && !error) { <mat-progress-bar mode="indeterminate" /> }
    @if (error) { <p class="muted" style="padding:16px">{{ error }}</p> }
  `,
  styles: [
    `
      .xd { padding: 4px 2px 24px; }
      .xd-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
      .back { display: inline-flex; align-items: center; gap: 4px; font-size: 0.85rem; color: var(--text-muted,#8a94a6); text-decoration: none; }
      .xd-head h2 { margin: 6px 0 4px; }
      .meta { display: flex; align-items: center; gap: 10px; }
      .muted { color: var(--text-muted,#8a94a6); }
      .small { font-size: 0.8rem; }
      .chip { text-transform: uppercase; font-size: 0.7rem; letter-spacing: .04em; padding: 2px 10px; border-radius: 10px; background: rgba(120,140,170,.2); }
      .chip.run { background: rgba(214,158,46,.25); }
      .chip.done { background: rgba(72,187,120,.25); }
      .chip.warn { background: rgba(214,158,46,.25); color: #d69e2e; font-weight: 700; }
      .run-box { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .score { margin: 16px 0; }
      .score-line { display: flex; justify-content: space-between; margin-bottom: 4px; }
      .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
      .grid .panel:first-child { grid-row: span 2; }
      .range-panel { grid-column: 1 / -1; }
      .panel { padding: 14px 16px; }
      .panel h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 10px; font-size: 1rem; }
      .obj { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid rgba(120,140,170,.12); }
      .obj mat-icon { color: var(--text-muted,#8a94a6); }
      .obj.ok mat-icon { color: #48bb78; }
      .obj-ref { font-weight: 600; }
      .obj-type { font-size: 0.78rem; color: var(--text-muted,#8a94a6); }
      .spacer { flex: 1 1 auto; }
      .pts { font-variant-numeric: tabular-nums; }
      .tl-step { display: flex; gap: 10px; padding: 6px 0; border-left: 2px solid rgba(129,140,248,.4); padding-left: 10px; margin-left: 4px; }
      .tl-t { font-variant-numeric: tabular-nums; color: var(--text-muted,#8a94a6); min-width: 34px; }
      .tl-tech { font-weight: 600; color: #818cf8; min-width: 52px; }
      .tl-ce { font-weight: 500; }
      .nf { margin-top: 12px; padding-top: 8px; border-top: 1px dashed rgba(120,140,170,.25); }
      .nf-h { font-size: 0.8rem; font-weight: 600; margin-bottom: 4px; }
      .topo { width: 100%; overflow: auto; border-radius: 10px; background: #071322; border: 1px solid rgba(120,140,170,.15); margin: 10px 0; }
      .topo-svg { width: 100%; height: auto; min-height: 240px; max-height: 460px; display: block; }
      .range-btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
      @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .grid .panel:first-child { grid-row: auto; } }
    `,
  ],
})
export class ExerciseDetailComponent implements OnInit, OnDestroy {
  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private snack = inject(MatSnackBar);

  id = '';
  detail: ScenarioDetail | null = null;
  rangeName = '';
  rangeState = '';
  running = false;
  error = '';
  svgZones: SvgZone[] = [];
  svgNodes: SvgNode[] = [];
  viewBox = '0 0 800 400';
  private poll: ReturnType<typeof setInterval> | null = null;

  iconFor(t: string): string {
    return NODE_ICON[t] ?? '•';
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

  private parseDiagram(diagram: { cells?: any[] } | null | undefined): void {
    const cells = diagram?.cells ?? [];
    const zones: SvgZone[] = [];
    const nodes: SvgNode[] = [];
    let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    for (const c of cells) {
      if (typeof c.type === 'string' && c.type.toLowerCase().includes('link')) continue;
      const nt = c.nodeType || 'workstation';
      const pos = c.position || { x: 0, y: 0 };
      const color = NODE_COLORS[nt] || '#66BB6A';
      if (nt === 'subnet' || nt === 'dmz') {
        const size = c.size || { width: 600, height: 150 };
        zones.push({ x: pos.x, y: pos.y, w: size.width, h: size.height, label: c.nodeData?.label || '', cidr: c.nodeData?.cidr || '', color });
        minX = Math.min(minX, pos.x); minY = Math.min(minY, pos.y);
        maxX = Math.max(maxX, pos.x + size.width); maxY = Math.max(maxY, pos.y + size.height);
      } else {
        nodes.push({ x: pos.x, y: pos.y, label: c.nodeData?.label || nt, ip: c.nodeData?.ip || '', type: nt, color });
        minX = Math.min(minX, pos.x); minY = Math.min(minY, pos.y);
        maxX = Math.max(maxX, pos.x + 150); maxY = Math.max(maxY, pos.y + 54);
      }
    }
    this.svgZones = zones;
    this.svgNodes = nodes;
    if (nodes.length || zones.length) {
      const pad = 26;
      this.viewBox = `${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`;
    }
  }

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
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
            next: r => { this.rangeName = r.name; this.rangeState = r.state; },
            error: () => {},
          });
          if (!this.svgNodes.length) {
            this.api.getRangeDiagram(d.range_id).subscribe({
              next: (res: any) => this.parseDiagram(res?.diagram_json ?? res),
              error: () => {},
            });
          }
        }
        if (d.state === 'running' && !this.poll) this.startPolling();
      },
      error: () => (this.error = 'Exercise not found.'),
    });
  }

  run(): void {
    this.running = true;
    this.api.runExercise(this.id).subscribe({
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

  genAar(): void {
    this.api.generateAAR(this.id).subscribe({
      next: () => this.snack.open('After-Action Report generated', '', { duration: 3000 }),
      error: () => this.snack.open('AAR generation failed', '', { duration: 3000 }),
    });
  }
}
