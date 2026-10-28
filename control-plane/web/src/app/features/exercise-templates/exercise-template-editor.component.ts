import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { ApiService } from '@core/services/api.service';
import {
  DELIVERY_METHODS, ExerciseMadeFromTemplate, ExerciseTemplate, ExerciseTemplatesApiService, SCALING_KINDS, TEAM_SIDES,
} from '@core/services/exercise-templates-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ScenarioSummary, TemplateSummary } from '@core/models';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { ProvenChipComponent } from '../../shared/components/proven-chip/proven-chip.component';
import { apiErrorMessage } from '../../shared/kb-errors';
import { CreateFromTemplateComponent } from './create-from-template.component';

type Side = (typeof TEAM_SIDES)[number];
type Delivery = (typeof DELIVERY_METHODS)[number];
interface TeamRow { name: string; side: Side; min_students: number; max_students: number }
interface VmRow { node_type: string; count: number }
interface RoleRow {
  name: string; description: string; team: string; min_participants: number; max_participants: number;
  default_count: number; vm_profile: VmRow[];
}
interface RuleRow {
  name: string; node_type: string; kind: (typeof SCALING_KINDS)[number]; count: number; per: number; role: string;
  segment: string;
}
interface ObjectiveRow { ref: string; text: string; moe: string; competency_code: string }
interface SerialRow {
  serial: number; phase: string; scenario_time: string; title: string; description: string; objective_ref: string;
  attack_technique: string; delivery_method: Delivery; from_cell: string; to_participant: string;
  expected_action: string; moe: string;
}

/**
 * The exercise template editor (ADR 0016): name, scenario and range template, teams,
 * objectives, the MESL and the instructor's notes; the Proven chip; "Create exercise" and the
 * exercises already made from it. Exercises are collective training, so there are teams and
 * no individual shape. A shared template of another tenant is read-only here.
 */
@Component({
  selector: 'tn-exercise-template-editor',
  imports: [
    DatePipe, FormsModule, RouterLink, MatButtonModule, MatCardModule, MatFormFieldModule, MatIconModule,
    MatInputModule, MatSelectModule, ProvenChipComponent, CreateFromTemplateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">library_books</mat-icon>
          <div>
            <div class="tn-kicker">Exercise template</div>
            <h1>{{ id() ? name || 'Exercise template' : 'New exercise template' }}</h1>
            @if (id()) { <p class="subtitle">Revision {{ revision() }}. Editing it never changes exercises already made from it.</p> }
          </div>
        </div>
        <a mat-button routerLink="/exercises/templates"><mat-icon>arrow_back</mat-icon> Templates</a>
      </div>

      @if (readOnly()) {
        <p class="note" role="note">Shared by another tenant: you can read it and create exercises from it, not change it.</p>
      }
      @if (error()) { <p class="err" role="alert">{{ error() }}</p> }

      @if (id()) {
        <mat-card class="mt-2">
          <mat-card-content>
            <tn-proven-chip itemType="exercise_template" [itemId]="id()!" />
          </mat-card-content>
        </mat-card>
      }

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>Details</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          <mat-form-field appearance="outline">
            <mat-label>Name</mat-label>
            <input matInput name="name" [(ngModel)]="name" [disabled]="readOnly()" required>
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Description</mat-label>
            <textarea matInput name="description" rows="3" [(ngModel)]="description" [disabled]="readOnly()"></textarea>
          </mat-form-field>
          <div class="row">
            <mat-form-field appearance="outline">
              <mat-label>Scenario</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="scenarioId" [disabled]="readOnly()">
                <mat-option [value]="null">None</mat-option>
                @for (s of scenarios(); track s.id) { <mat-option [value]="s.id">{{ s.name }}</mat-option> }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Range template</mat-label>
              <mat-select panelClass="tn-select-panel" [(ngModel)]="rangeTemplateId" [disabled]="readOnly()">
                <mat-option [value]="null">None</mat-option>
                @for (t of rangeTemplates(); track t.id) { <mat-option [value]="t.id">{{ t.name }}</mat-option> }
              </mat-select>
              <mat-hint>A template with no range template cannot be proven.</mat-hint>
            </mat-form-field>
          </div>
          <mat-form-field appearance="outline">
            <mat-label>Tags (comma separated)</mat-label>
            <input matInput name="tags" [(ngModel)]="tags" [disabled]="readOnly()">
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Instructor notes</mat-label>
            <textarea matInput name="notes" rows="4" [(ngModel)]="notes" [disabled]="readOnly()"></textarea>
          </mat-form-field>
        </mat-card-content>
      </mat-card>

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>Teams</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          @for (t of teams(); track $index) {
            <div class="row">
              <mat-form-field appearance="outline">
                <mat-label>Team</mat-label>
                <input matInput [name]="'team' + $index" [(ngModel)]="t.name" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Side</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="t.side" [disabled]="readOnly()">
                  @for (s of sides; track s) { <mat-option [value]="s">{{ s }}</mat-option> }
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline" class="num">
                <mat-label>Min Students</mat-label>
                <input matInput type="number" min="0" [name]="'tmin' + $index" [(ngModel)]="t.min_students" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline" class="num">
                <mat-label>Max (0 = any)</mat-label>
                <input matInput type="number" min="0" [name]="'tmax' + $index" [(ngModel)]="t.max_students" [disabled]="readOnly()">
              </mat-form-field>
              @if (!readOnly()) {
                <button mat-icon-button type="button" (click)="remove(teams, $index)" aria-label="Remove team"><mat-icon>delete</mat-icon></button>
              }
            </div>
          }
          @if (!readOnly()) {
            <div><button mat-stroked-button type="button" (click)="addTeam()"><mat-icon>add</mat-icon> Add team</button></div>
          }
        </mat-card-content>
      </mat-card>

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>Participant roles</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          <p class="hint">How many Students may take each role, and the machines each one gets (node types of the range template).
            The Exercise Studio sizes an exercise from these.</p>
          @for (r of roles(); track $index) {
            <div class="role">
              <div class="row">
                <mat-form-field appearance="outline">
                  <mat-label>Role</mat-label>
                  <input matInput [name]="'rname' + $index" [(ngModel)]="r.name" [disabled]="readOnly()" placeholder="Analyst">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Team</mat-label>
                  <mat-select panelClass="tn-select-panel" [(ngModel)]="r.team" [disabled]="readOnly()">
                    <mat-option value="">None</mat-option>
                    @for (t of teams(); track $index) { <mat-option [value]="t.name">{{ t.name }}</mat-option> }
                  </mat-select>
                </mat-form-field>
                <mat-form-field appearance="outline" class="num">
                  <mat-label>Min</mat-label>
                  <input matInput type="number" min="0" [name]="'rmin' + $index" [(ngModel)]="r.min_participants" [disabled]="readOnly()">
                </mat-form-field>
                <mat-form-field appearance="outline" class="num">
                  <mat-label>Max (0 = any)</mat-label>
                  <input matInput type="number" min="0" [name]="'rmax' + $index" [(ngModel)]="r.max_participants" [disabled]="readOnly()">
                </mat-form-field>
                <mat-form-field appearance="outline" class="num">
                  <mat-label>Default</mat-label>
                  <input matInput type="number" min="0" [name]="'rdef' + $index" [(ngModel)]="r.default_count" [disabled]="readOnly()">
                </mat-form-field>
                @if (!readOnly()) {
                  <button mat-icon-button type="button" (click)="remove(roles, $index)" aria-label="Remove role"><mat-icon>delete</mat-icon></button>
                }
              </div>
              <mat-form-field appearance="outline" class="full">
                <mat-label>Description</mat-label>
                <input matInput [name]="'rdesc' + $index" [(ngModel)]="r.description" [disabled]="readOnly()">
              </mat-form-field>
              <div class="sub">Each participant gets:</div>
              @for (v of r.vm_profile; track $index; let vi = $index) {
                <div class="row">
                  <mat-form-field appearance="outline" class="grow">
                    <mat-label>Node type</mat-label>
                    <input matInput [name]="'rvm' + $index + '-' + vi" [(ngModel)]="v.node_type" [disabled]="readOnly()" placeholder="ubuntu-2404">
                  </mat-form-field>
                  <mat-form-field appearance="outline" class="num">
                    <mat-label>Count</mat-label>
                    <input matInput type="number" min="1" [name]="'rvmc' + $index + '-' + vi" [(ngModel)]="v.count" [disabled]="readOnly()">
                  </mat-form-field>
                  @if (!readOnly()) {
                    <button mat-icon-button type="button" (click)="removeVm(r, vi)" aria-label="Remove machine"><mat-icon>delete</mat-icon></button>
                  }
                </div>
              }
              @if (!readOnly()) {
                <div><button mat-button type="button" (click)="addVm(r)"><mat-icon>add</mat-icon> Add machine</button></div>
              }
            </div>
          }
          @if (!readOnly()) {
            <div><button mat-stroked-button type="button" (click)="addRole()"><mat-icon>add</mat-icon> Add role</button></div>
          }
        </mat-card-content>
      </mat-card>

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>Shared infrastructure scaling</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          <p class="hint">Machines that scale with the exercise: per network segment, per N participants (optionally of one role), or fixed.</p>
          @for (r of rules(); track $index) {
            <div class="row">
              <mat-form-field appearance="outline">
                <mat-label>Name</mat-label>
                <input matInput [name]="'sname' + $index" [(ngModel)]="r.name" [disabled]="readOnly()" placeholder="Sensor">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Node type</mat-label>
                <input matInput [name]="'snode' + $index" [(ngModel)]="r.node_type" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Scales</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="r.kind" [disabled]="readOnly()">
                  @for (k of kinds; track k) { <mat-option [value]="k">{{ kindLabel(k) }}</mat-option> }
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline" class="num">
                <mat-label>Count</mat-label>
                <input matInput type="number" min="1" [name]="'scount' + $index" [(ngModel)]="r.count" [disabled]="readOnly()">
              </mat-form-field>
              @if (r.kind === 'per_participants') {
                <mat-form-field appearance="outline" class="num">
                  <mat-label>Per N</mat-label>
                  <input matInput type="number" min="1" [name]="'sper' + $index" [(ngModel)]="r.per" [disabled]="readOnly()">
                </mat-form-field>
                <mat-form-field appearance="outline">
                  <mat-label>Of role</mat-label>
                  <mat-select panelClass="tn-select-panel" [(ngModel)]="r.role" [disabled]="readOnly()">
                    <mat-option value="">All participants</mat-option>
                    @for (ro of roles(); track $index) { <mat-option [value]="ro.name">{{ ro.name }}</mat-option> }
                  </mat-select>
                </mat-form-field>
              }
              @if (!readOnly()) {
                <button mat-icon-button type="button" (click)="remove(rules, $index)" aria-label="Remove rule"><mat-icon>delete</mat-icon></button>
              }
            </div>
          }
          @if (!readOnly()) {
            <div><button mat-stroked-button type="button" (click)="addRule()"><mat-icon>add</mat-icon> Add rule</button></div>
          }
        </mat-card-content>
      </mat-card>

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>Objectives</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          @for (o of objectives(); track $index) {
            <div class="row">
              <mat-form-field appearance="outline" class="num">
                <mat-label>Ref</mat-label>
                <input matInput [name]="'oref' + $index" [(ngModel)]="o.ref" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline" class="grow">
                <mat-label>Objective</mat-label>
                <input matInput [name]="'otext' + $index" [(ngModel)]="o.text" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline" class="grow">
                <mat-label>Measure of effectiveness</mat-label>
                <input matInput [name]="'omoe' + $index" [(ngModel)]="o.moe" [disabled]="readOnly()">
              </mat-form-field>
              @if (!readOnly()) {
                <button mat-icon-button type="button" (click)="remove(objectives, $index)" aria-label="Remove objective"><mat-icon>delete</mat-icon></button>
              }
            </div>
          }
          @if (!readOnly()) {
            <div><button mat-stroked-button type="button" (click)="addObjective()"><mat-icon>add</mat-icon> Add objective</button></div>
          }
        </mat-card-content>
      </mat-card>

      <mat-card class="mt-2">
        <mat-card-header><mat-card-title>MESL</mat-card-title></mat-card-header>
        <mat-card-content class="col">
          @for (m of mesl(); track $index) {
            <div class="row">
              <mat-form-field appearance="outline" class="num">
                <mat-label>Serial</mat-label>
                <input matInput type="number" min="0" [name]="'mserial' + $index" [(ngModel)]="m.serial" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline" class="num">
                <mat-label>Time</mat-label>
                <input matInput [name]="'mtime' + $index" [(ngModel)]="m.scenario_time" [disabled]="readOnly()" placeholder="D1 0900">
              </mat-form-field>
              <mat-form-field appearance="outline" class="grow">
                <mat-label>Title</mat-label>
                <input matInput [name]="'mtitle' + $index" [(ngModel)]="m.title" [disabled]="readOnly()">
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Delivery</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="m.delivery_method" [disabled]="readOnly()">
                  @for (d of deliveries; track d) { <mat-option [value]="d">{{ d }}</mat-option> }
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline" class="num">
                <mat-label>Objective</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="m.objective_ref" [disabled]="readOnly()">
                  <mat-option value="">None</mat-option>
                  @for (o of objectives(); track $index) { <mat-option [value]="o.ref">{{ o.ref }}</mat-option> }
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline" class="grow">
                <mat-label>Expected action</mat-label>
                <input matInput [name]="'maction' + $index" [(ngModel)]="m.expected_action" [disabled]="readOnly()">
              </mat-form-field>
              @if (!readOnly()) {
                <button mat-icon-button type="button" (click)="remove(mesl, $index)" aria-label="Remove serial"><mat-icon>delete</mat-icon></button>
              }
            </div>
          }
          @if (!readOnly()) {
            <div><button mat-stroked-button type="button" (click)="addSerial()"><mat-icon>add</mat-icon> Add serial</button></div>
          }
        </mat-card-content>
      </mat-card>

      <div class="bar">
        @if (!readOnly()) {
          <button mat-raised-button color="primary" (click)="save()" [disabled]="!name.trim() || saving()">
            {{ saving() ? 'Saving...' : 'Save' }}
          </button>
        }
        @if (id()) {
          <button mat-stroked-button (click)="creating.set(!creating())"><mat-icon>play_circle</mat-icon> Create exercise from template</button>
          @if (!readOnly()) {
            <button mat-button color="warn" (click)="confirmDelete()">Delete</button>
          }
        }
      </div>

      @if (id() && creating()) {
        <mat-card class="mt-2">
          <mat-card-header><mat-card-title>Create exercise from this template</mat-card-title></mat-card-header>
          <mat-card-content>
            <tn-create-from-template [templateId]="id()!" [templateName]="name" (cancelled)="creating.set(false)" />
          </mat-card-content>
        </mat-card>
      }

      @if (id() && made().length) {
        <mat-card class="mt-2">
          <mat-card-header><mat-card-title>Exercises made from it</mat-card-title></mat-card-header>
          <mat-card-content>
            <ul class="made">
              @for (e of made(); track e.id) {
                <li>
                  <a [routerLink]="['/exercises', e.id]">{{ e.name }}</a>
                  <span class="sub">{{ e.state }} · {{ e.total_score }}/{{ e.max_score }} · from revision
                    {{ e.template_revision }} · {{ e.created_at | date: 'd MMM y' }}</span>
                </li>
              }
            </ul>
          </mat-card-content>
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
    .col { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start; }
    .row mat-form-field { flex: 1 1 180px; }
    .row mat-form-field.num { flex: 0 1 110px; }
    .row mat-form-field.grow { flex: 2 1 240px; }
    .role { display: flex; flex-direction: column; gap: 4px; padding: 8px 0; border-bottom: 1px solid var(--border); }
    .full { width: 100%; }
    .hint { margin: 0; font-size: 13px; color: var(--text-muted); }
    .bar { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
    .note { padding: 8px 12px; border-radius: 8px; background: var(--mat-sys-surface-container-high); font-size: 13px; }
    .err { color: var(--mat-sys-error); font-size: 13px; }
    .made { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    .sub { margin-left: 8px; font-size: 12px; color: var(--text-muted); }
  `],
})
export class ExerciseTemplateEditorComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly templatesApi = inject(ExerciseTemplatesApiService);
  private readonly api = inject(ApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);

  protected readonly sides = TEAM_SIDES;
  protected readonly deliveries = DELIVERY_METHODS;
  protected readonly kinds = SCALING_KINDS;

  readonly id = signal<string | null>(null);
  readonly revision = signal(1);
  readonly readOnly = signal(false);
  readonly saving = signal(false);
  readonly creating = signal(false);
  readonly error = signal('');
  readonly scenarios = signal<ScenarioSummary[]>([]);
  readonly rangeTemplates = signal<TemplateSummary[]>([]);
  readonly made = signal<ExerciseMadeFromTemplate[]>([]);
  readonly teams = signal<TeamRow[]>([]);
  readonly roles = signal<RoleRow[]>([]);
  readonly rules = signal<RuleRow[]>([]);
  readonly objectives = signal<ObjectiveRow[]>([]);
  readonly mesl = signal<SerialRow[]>([]);
  name = '';
  description = '';
  notes = '';
  tags = '';
  scenarioId: string | null = null;
  rangeTemplateId: string | null = null;

  ngOnInit(): void {
    this.api.listScenarios(200).subscribe(s => this.scenarios.set(s));
    this.api.listTemplates(200).subscribe(t => this.rangeTemplates.set(t));
    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') this.load(id);
  }

  private load(id: string): void {
    this.templatesApi.get(id).subscribe({
      next: t => { this.apply(t); this.loadMade(id); },
      error: err => this.error.set(apiErrorMessage(err, 'The template could not be loaded.')),
    });
  }

  private loadMade(id: string): void {
    this.templatesApi.exercises(id).subscribe({ next: m => this.made.set(m), error: () => undefined });
  }

  private apply(t: ExerciseTemplate): void {
    this.id.set(t.id);
    this.revision.set(t.revision);
    this.readOnly.set(!!t.shared);
    this.name = t.name;
    this.description = t.description;
    this.notes = t.instructor_notes;
    this.tags = (t.tags ?? []).join(', ');
    this.scenarioId = t.scenario_id ?? null;
    this.rangeTemplateId = t.range_template_id ?? null;
    this.teams.set(t.teams.map(x => ({
      name: x.name, side: x.side ?? 'other', min_students: x.min_students ?? 0, max_students: x.max_students ?? 0 })));
    this.roles.set((t.roles ?? []).map(r => ({
      name: r.name, description: r.description ?? '', team: r.team ?? '', min_participants: r.min_participants ?? 0,
      max_participants: r.max_participants ?? 0, default_count: r.default_count ?? 0,
      vm_profile: (r.vm_profile ?? []).map(v => ({ node_type: v.node_type, count: v.count ?? 1 })) })));
    this.rules.set((t.scaling_rules ?? []).map(r => ({
      name: r.name, node_type: r.node_type, kind: r.kind ?? 'fixed', count: r.count ?? 1, per: r.per ?? 0,
      role: r.role ?? '', segment: r.segment ?? '' })));
    this.objectives.set(t.objectives.map(o => ({
      ref: o.ref, text: o.text ?? '', moe: o.moe ?? '', competency_code: o.competency_code ?? '' })));
    this.mesl.set(t.mesl.map(m => ({
      serial: m.serial, phase: m.phase ?? '', scenario_time: m.scenario_time ?? '', title: m.title,
      description: m.description ?? '', objective_ref: m.objective_ref ?? '', attack_technique: m.attack_technique ?? '',
      delivery_method: m.delivery_method ?? 'cyber', from_cell: m.from_cell ?? '', to_participant: m.to_participant ?? '',
      expected_action: m.expected_action ?? '', moe: m.moe ?? '' })));
  }

  remove<T>(list: { update: (f: (v: T[]) => T[]) => void }, index: number): void {
    list.update(v => v.filter((_, i) => i !== index));
  }
  addTeam(): void {
    this.teams.update(v => [...v, { name: '', side: 'blue', min_students: 0, max_students: 0 }]);
  }
  addRole(): void {
    this.roles.update(v => [...v, { name: '', description: '', team: '', min_participants: 0, max_participants: 0,
      default_count: 0, vm_profile: [] }]);
  }
  addVm(role: RoleRow): void {
    role.vm_profile = [...role.vm_profile, { node_type: '', count: 1 }];
  }
  removeVm(role: RoleRow, index: number): void {
    role.vm_profile = role.vm_profile.filter((_, i) => i !== index);
  }
  addRule(): void {
    this.rules.update(v => [...v, { name: '', node_type: '', kind: 'fixed', count: 1, per: 0, role: '', segment: '' }]);
  }
  kindLabel(kind: string): string {
    return { per_segment: 'Per segment', per_participants: 'Per N participants', fixed: 'Fixed' }[kind] ?? kind;
  }
  addObjective(): void {
    this.objectives.update(v => [...v, { ref: `O${v.length + 1}`, text: '', moe: '', competency_code: '' }]);
  }
  addSerial(): void {
    this.mesl.update(v => [...v, {
      serial: v.reduce((n, m) => Math.max(n, m.serial), 0) + 1, phase: '', scenario_time: '', title: '', description: '',
      objective_ref: '', attack_technique: '', delivery_method: 'cyber', from_cell: '', to_participant: '',
      expected_action: '', moe: '' }]);
  }

  save(): void {
    if (this.saving() || this.readOnly()) return;
    this.saving.set(true);
    this.error.set('');
    const body = {
      name: this.name.trim(), description: this.description, instructor_notes: this.notes,
      tags: this.tags.split(',').map(t => t.trim()).filter(Boolean),
      scenario_id: this.scenarioId, range_template_id: this.rangeTemplateId,
      teams: this.teams(), roles: this.roles(), scaling_rules: this.rules().map(r => this.cleanRule(r)),
      objectives: this.objectives(), mesl: this.mesl(),
    };
    const id = this.id();
    const request = id ? this.templatesApi.update(id, body) : this.templatesApi.create(body);
    request.subscribe({
      next: t => {
        this.saving.set(false);
        this.notify.success('Exercise template saved');
        if (!id) void this.router.navigate(['/exercises/templates', t.id]);
        else this.apply(t);
      },
      error: err => {
        this.saving.set(false);
        this.error.set(apiErrorMessage(err, 'The template could not be saved.'));
      },
    });
  }

  /** The API refuses `per`/`role` on a rule that is not per-participants. */
  private cleanRule(r: RuleRow): RuleRow {
    return r.kind === 'per_participants' ? { ...r, segment: '' } : { ...r, per: 0, role: '', segment: r.kind === 'per_segment' ? r.segment : '' };
  }

  confirmDelete(): void {
    const id = this.id();
    if (!id) return;
    this.dialog
      .open(ConfirmDialogComponent, {
        data: { title: 'Delete exercise template', message: `Delete "${this.name}"?`, confirmText: 'Delete' },
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.templatesApi.remove(id).subscribe({
          next: () => { this.notify.success('Exercise template deleted'); void this.router.navigate(['/exercises/templates']); },
          error: err => this.error.set(apiErrorMessage(err, 'Delete failed.')),
        });
      });
  }
}
