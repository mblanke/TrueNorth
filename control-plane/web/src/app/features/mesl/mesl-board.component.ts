import { Component, Inject, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MAT_DIALOG_DATA, MatDialog, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService, CollectiveExerciseDetail, MeslEvent } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { EnterStaggerDirective } from '../../shared/motion';

/** The vocabularies the API enforces — mirrored so the UI cannot author invalid values. */
export const DELIVERY_METHODS = ['cyber', 'white_cell', 'email', 'radio', 'physical', 'opfor'] as const;
export const MESL_STATUSES = ['planned', 'staged', 'delivered', 'responded', 'skipped'] as const;

const DELIVERY_ICON: Record<string, string> = {
  cyber: 'lan',
  white_cell: 'admin_panel_settings',
  email: 'mail',
  radio: 'radio',
  physical: 'directions_walk',
  opfor: 'swords',
};

/** Status → the shared chip vocabulary, so the board reads like the rest of the app. */
const STATUS_CHIP: Record<string, string> = {
  planned: 'draft',
  staged: 'pending',
  delivered: 'running',
  responded: 'completed',
  skipped: 'disabled',
};

export function groupByPhase(events: MeslEvent[]): { phase: string; events: MeslEvent[] }[] {
  const groups = new Map<string, MeslEvent[]>();
  for (const e of [...events].sort((a, b) => a.serial - b.serial)) {
    const key = e.phase?.trim() || 'Unphased';
    const list = groups.get(key);
    if (list) list.push(e);
    else groups.set(key, [e]);
  }
  return [...groups.entries()].map(([phase, evts]) => ({ phase, events: evts }));
}

/** Edit one serial. */
@Component({
  selector: 'tn-mesl-event-dialog',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule,
    MatInputModule, MatSelectModule,
  ],
  template: `
    <h2 mat-dialog-title>Serial {{ event.serial }}</h2>
    <mat-dialog-content>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Title</mat-label>
        <input matInput name="title" [(ngModel)]="event.title" />
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Description</mat-label>
        <textarea matInput rows="3" name="description" [(ngModel)]="event.description"></textarea>
      </mat-form-field>
      <div class="two">
        <mat-form-field appearance="outline">
          <mat-label>Phase</mat-label>
          <input matInput name="phase" [(ngModel)]="event.phase" />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Scenario time</mat-label>
          <input matInput name="scenario_time" [(ngModel)]="event.scenario_time" placeholder="D1 0800" />
        </mat-form-field>
      </div>
      <div class="two">
        <mat-form-field appearance="outline">
          <mat-label>Delivery method</mat-label>
          <mat-select name="delivery_method" [(ngModel)]="event.delivery_method" panelClass="tn-select-panel">
            @for (d of deliveryMethods; track d) {
              <mat-option [value]="d">{{ d }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Status</mat-label>
          <mat-select name="status" [(ngModel)]="event.status" panelClass="tn-select-panel">
            @for (s of statuses; track s) {
              <mat-option [value]="s">{{ s }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
      </div>
      <div class="two">
        <mat-form-field appearance="outline">
          <mat-label>Objective ref</mat-label>
          <input matInput name="objective_ref" [(ngModel)]="event.objective_ref" />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>ATT&CK technique</mat-label>
          <input matInput name="attack_technique" [(ngModel)]="event.attack_technique" placeholder="T1566.001" />
        </mat-form-field>
      </div>
      <div class="two">
        <mat-form-field appearance="outline">
          <mat-label>From cell</mat-label>
          <input matInput name="from_cell" [(ngModel)]="event.from_cell" />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>To participant</mat-label>
          <input matInput name="to_participant" [(ngModel)]="event.to_participant" />
        </mat-form-field>
      </div>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Expected action</mat-label>
        <input matInput name="expected_action" [(ngModel)]="event.expected_action" />
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Measure of effectiveness</mat-label>
        <input matInput name="moe" [(ngModel)]="event.moe" />
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="ref.close()">Cancel</button>
      <button mat-raised-button color="primary" (click)="ref.close(event)">Save serial</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .w { width: 100%; display: block; }
    .two { display: flex; gap: 12px; flex-wrap: wrap; }
    .two mat-form-field { flex: 1; min-width: 180px; }
  `],
})
export class MeslEventDialogComponent {
  protected readonly deliveryMethods = DELIVERY_METHODS;
  protected readonly statuses = MESL_STATUSES;
  event: MeslEvent;

  constructor(
    public ref: MatDialogRef<MeslEventDialogComponent, MeslEvent>,
    @Inject(MAT_DIALOG_DATA) data: { event: MeslEvent },
  ) {
    this.event = { ...data.event };
  }
}

/** Draft a MESL from the exercise objectives. */
@Component({
  selector: 'tn-mesl-generate-dialog',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule,
    MatInputModule, MatIconModule,
  ],
  template: `
    <h2 mat-dialog-title>Generate MESL</h2>
    <mat-dialog-content>
      <p class="warn"><mat-icon>warning</mat-icon>
        This replaces every serial currently on the exercise.</p>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Number of events</mat-label>
        <input matInput type="number" min="3" max="60" name="count" [(ngModel)]="eventCount" />
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Adversary / package</mat-label>
        <input matInput name="adversary" [(ngModel)]="adversary" placeholder="APT29-style intrusion set" />
      </mat-form-field>
      <mat-form-field appearance="outline" class="w">
        <mat-label>Duration (days)</mat-label>
        <input matInput type="number" min="1" max="5" name="days" [(ngModel)]="durationDays" />
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="ref.close()">Cancel</button>
      <button mat-raised-button color="primary" (click)="submit()">Generate</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .w { width: 100%; display: block; }
    .warn {
      display: flex; align-items: center; gap: 8px; margin: 0 0 12px;
      color: var(--warning); font-size: 0.86rem;
    }
  `],
})
export class MeslGenerateDialogComponent {
  eventCount = 12;
  adversary = '';
  durationDays = 1;

  constructor(public ref: MatDialogRef<MeslGenerateDialogComponent>) {}

  submit(): void {
    this.ref.close({
      event_count: Math.min(60, Math.max(3, Number(this.eventCount) || 12)),
      adversary: this.adversary.trim(),
      duration_days: Math.min(5, Math.max(1, Number(this.durationDays) || 1)),
    });
  }
}

/**
 * The MESL board: serials grouped by phase, each editable, with the planner CSV
 * importer and the AI drafter wired in. The backend has carried all of this
 * since it was written; this is its first interface.
 */
@Component({
  selector: 'tn-mesl-board',
  standalone: true,
  imports: [
    CommonModule, RouterLink, MatButtonModule, MatCardModule, MatDialogModule,
    MatIconModule, MatTooltipModule, EmptyStateComponent, EnterStaggerDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <a mat-icon-button routerLink="/authoring/mesl" matTooltip="Back to exercises">
            <mat-icon>arrow_back</mat-icon>
          </a>
          <div>
            <h1>{{ detail()?.name || 'MESL' }}</h1>
            <p class="subtitle">{{ events().length }} serials · {{ objectives().length }} objectives</p>
          </div>
        </div>
        <div class="header-actions">
          <button mat-stroked-button (click)="importObjectives.click()">
            <mat-icon>upload_file</mat-icon> Import objectives
          </button>
          <button mat-stroked-button (click)="importMesl.click()">
            <mat-icon>upload</mat-icon> Import MESL
          </button>
          <button mat-raised-button color="primary" (click)="generate()" [disabled]="generating()">
            <mat-icon>auto_fix_high</mat-icon> {{ generating() ? 'Generating…' : 'Generate MESL' }}
          </button>
        </div>
      </div>

      <input #importObjectives type="file" accept=".csv" hidden (change)="onObjectivesFile($event)" />
      <input #importMesl type="file" accept=".csv" hidden (change)="onMeslFile($event)" />

      <div class="board">
        <div class="serials">
          @if (loading() || generating()) {
            <div class="tn-skeleton-group" aria-busy="true">
              <div class="tn-skeleton tn-skeleton-row"></div>
              <div class="tn-skeleton tn-skeleton-row"></div>
              <div class="tn-skeleton tn-skeleton-row"></div>
            </div>
          } @else if (!events().length) {
            <tn-empty-state icon="list_alt" title="No serials yet"
                            message="Import a planner MESL CSV, or draft one from the objectives with AI.">
              <button mat-stroked-button (click)="generate()">
                <mat-icon>auto_fix_high</mat-icon> Generate MESL
              </button>
            </tn-empty-state>
          } @else {
            @for (group of phases(); track group.phase) {
              <section class="phase">
                <h2 class="phase-head">
                  <span class="phase-name">{{ group.phase }}</span>
                  <span class="phase-count">{{ group.events.length }}</span>
                </h2>
                <div tnEnterStagger>
                  @for (e of group.events; track e.id) {
                    <mat-card class="serial tn-stagger-item">
                      <mat-card-content>
                        <div class="serial-top">
                          <span class="serial-no">#{{ e.serial }}</span>
                          <span class="serial-time">{{ e.scenario_time }}</span>
                          <span class="serial-title">{{ e.title }}</span>
                          <span class="spacer"></span>
                          <span class="delivery" [matTooltip]="e.delivery_method">
                            <mat-icon>{{ deliveryIcon(e.delivery_method) }}</mat-icon>
                          </span>
                          <button class="chip-button" (click)="cycleStatus(e)"
                                  matTooltip="Advance delivery status">
                            <span class="status-chip" [class]="statusChip(e.status)">{{ e.status }}</span>
                          </button>
                          <button mat-icon-button (click)="edit(e)" matTooltip="Edit serial">
                            <mat-icon>edit</mat-icon>
                          </button>
                        </div>
                        @if (e.description) { <p class="serial-desc">{{ e.description }}</p> }
                        <div class="serial-meta">
                          @if (e.objective_ref) { <span class="status-chip sev-info">{{ e.objective_ref }}</span> }
                          @if (e.attack_technique) { <span class="status-chip sev-low">{{ e.attack_technique }}</span> }
                          @if (e.to_participant) { <span class="muted">to {{ e.to_participant }}</span> }
                          @if (e.expected_action) { <span class="muted">expects {{ e.expected_action }}</span> }
                        </div>
                      </mat-card-content>
                    </mat-card>
                  }
                </div>
              </section>
            }
          }
        </div>

        <aside class="objectives">
          <mat-card>
            <mat-card-content>
              <h2 class="section-heading">Objectives</h2>
              @if (!objectives().length) {
                <p class="muted">None yet — import a CSV or add them when creating the exercise.</p>
              }
              @for (o of objectives(); track o.id) {
                <div class="obj">
                  <span class="status-chip sev-info">{{ o.ref }}</span>
                  <div class="obj-body">
                    <span class="obj-text">{{ o.text }}</span>
                    @if (o.moe) { <span class="muted">MOE: {{ o.moe }}</span> }
                  </div>
                </div>
              }
            </mat-card-content>
          </mat-card>
        </aside>
      </div>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .spacer { flex: 1 1 auto; }
    .muted { color: var(--text-muted); font-size: 0.78rem; }

    .board { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; margin-top: 16px; align-items: start; }
    .serials { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
    .phase-head { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font-size: 0.9rem; }
    .phase-name { text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-secondary); }
    .phase-count { font-size: 0.74rem; color: var(--text-muted); }
    .serial { margin-bottom: 8px; }
    .serial-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .serial-no { font-family: var(--font-mono); font-size: 0.78rem; color: var(--text-muted); }
    .serial-time { font-family: var(--font-mono); font-size: 0.78rem; color: var(--accent); }
    .serial-title { font-weight: 600; color: var(--text-primary); }
    .delivery mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-secondary); }
    .chip-button { background: none; border: 0; padding: 0; cursor: pointer; font: inherit; }
    .serial-desc { margin: 6px 0 4px; font-size: 0.84rem; color: var(--text-secondary); }
    .serial-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

    .objectives .obj { display: flex; gap: 8px; align-items: flex-start; padding: 6px 0; border-top: 1px solid var(--border); }
    .objectives .obj:first-of-type { border-top: 0; }
    .obj-body { display: flex; flex-direction: column; }
    .obj-text { font-size: 0.84rem; color: var(--text-primary); }
    .section-heading { margin: 0 0 8px; font-size: 0.95rem; }

    @media (max-width: 1000px) { .board { grid-template-columns: 1fr; } }
  `],
})
export class MeslBoardComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);
  private readonly route = inject(ActivatedRoute);

  protected readonly detail = signal<CollectiveExerciseDetail | null>(null);
  protected readonly loading = signal(true);
  protected readonly generating = signal(false);

  protected readonly events = computed(() => this.detail()?.mesl ?? []);
  protected readonly objectives = computed(() => this.detail()?.objectives ?? []);
  protected readonly phases = computed(() => groupByPhase(this.events()));

  private exerciseId = '';

  ngOnInit(): void {
    this.exerciseId = this.route.snapshot.paramMap.get('id') ?? '';
    this.load();
  }

  private load(): void {
    if (!this.exerciseId) { this.loading.set(false); return; }
    this.api.getCollectiveExercise(this.exerciseId).subscribe({
      next: d => { this.detail.set(d); this.loading.set(false); },
      error: () => { this.loading.set(false); this.notify.error('Could not load that exercise'); },
    });
  }

  protected deliveryIcon(method: string): string {
    return DELIVERY_ICON[method] ?? 'bolt';
  }

  protected statusChip(status: string): string {
    return STATUS_CHIP[status] ?? 'draft';
  }

  /** Quick advance along the delivery workflow, without opening the editor. */
  protected cycleStatus(e: MeslEvent): void {
    const order = MESL_STATUSES as readonly string[];
    const next = order[(Math.max(0, order.indexOf(e.status)) + 1) % order.length];
    this.patch(e, { status: next });
  }

  protected edit(e: MeslEvent): void {
    this.dialog
      .open(MeslEventDialogComponent, { data: { event: e }, width: '720px' })
      .afterClosed()
      .subscribe((updated?: MeslEvent) => {
        if (!updated) return;
        // id and serial are identity, not editable content.
        const { id: _id, serial: _serial, ...patch } = updated;
        this.patch(e, patch);
      });
  }

  private patch(e: MeslEvent, patch: Partial<MeslEvent>): void {
    this.api.patchMeslEvent(this.exerciseId, e.id, patch).subscribe({
      next: saved => {
        this.detail.update(d => d ? { ...d, mesl: d.mesl.map(m => (m.id === saved.id ? saved : m)) } : d);
      },
      error: err => this.notify.error(err?.error?.detail ?? 'Could not save that serial'),
    });
  }

  protected onObjectivesFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    this.api.importCollectiveObjectivesCsv(this.exerciseId, file).subscribe({
      next: res => { this.notify.success(`${res.objectives_added} objectives imported`); this.load(); },
      error: err => this.notify.error(err?.error?.detail ?? 'Objective import failed'),
    });
  }

  protected onMeslFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    // The endpoint replaces the MESL wholesale — say so before it happens.
    this.dialog
      .open(ConfirmDialogComponent, {
        data: {
          title: 'Replace the MESL?',
          message: 'Importing replaces every serial currently on this exercise.',
          confirmText: 'Import',
        },
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.api.importMeslCsv(this.exerciseId, file).subscribe({
          next: res => { this.notify.success(`${res.serials} serials imported`); this.load(); },
          error: err => this.notify.error(err?.error?.detail ?? 'MESL import failed'),
        });
      });
  }

  protected generate(): void {
    if (this.generating()) return;
    this.dialog.open(MeslGenerateDialogComponent).afterClosed().subscribe(req => {
      if (!req) return;
      this.generating.set(true);
      this.api.generateMesl(this.exerciseId, req).subscribe({
        next: res => {
          this.generating.set(false);
          this.notify.success(`${res.serials} serials drafted by ${res.model_used || 'the model'}`);
          this.load();
        },
        error: err => {
          this.generating.set(false);
          this.notify.error(err?.error?.detail ?? 'MESL generation failed');
        },
      });
    });
  }
}
