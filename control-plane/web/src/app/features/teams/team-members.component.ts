import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { AuthService } from '@core/services/auth.service';
import { roleCan } from '@core/auth/permissions';
import { NotificationService } from '@core/services/notification.service';
import {
  DirectoryUser, MyClasses, OperatorRecordsApiService, TeamMember, TeamSummary,
} from '@core/services/operator-records-api.service';
import {
  ImpactDialogComponent, ImpactDialogData, ImpactDialogResult,
} from '../../shared/components/impact-dialog/impact-dialog.component';

function apiError(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (typeof err.error?.detail === 'string') return err.error.detail;
    if (err.status === 403) return 'Your role does not allow this.';
    if (err.status === 0) return 'The API could not be reached.';
    return `The API answered ${err.status}.`;
  }
  return 'The request failed.';
}

/**
 * Team membership (R0-B). Instructors and admins see who is on each team (user:read).
 * Admins change any team (user:update). An instructor (team:manage_own, #200) creates
 * teams for the exercises they run (GET /class-setup/mine) and puts Students on them;
 * other teams stay read-only for them. The API enforces all of it; this only shows it.
 * Standing teams are created by admins on Users & Teams.
 */
@Component({
  selector: 'tn-team-members',
  imports: [CommonModule, FormsModule, MatButtonModule, MatCardModule, MatFormFieldModule, MatIconModule,
    MatInputModule, MatSelectModule],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">groups</mat-icon>
          <div>
            <h1>Team membership</h1>
            <p class="subtitle">Who is on each team. {{ subtitle() }}</p>
          </div>
        </div>
      </div>

      @if (myExercises().length) {
        <mat-card class="create">
          <form class="add" (ngSubmit)="create()" aria-label="New team for an exercise you run">
            <mat-form-field appearance="outline" class="grow">
              <mat-label>Exercise you run</mat-label>
              <mat-select name="exercise" [(ngModel)]="newExerciseId" panelClass="tn-select-panel" required>
                @for (x of myExercises(); track x.id) {
                  <mat-option [value]="x.id">{{ x.name }}</mat-option>
                }
              </mat-select>
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Team name</mat-label>
              <input matInput name="teamName" [(ngModel)]="newTeamName" maxlength="255" required>
            </mat-form-field>
            <button mat-raised-button color="primary" type="submit"
                    [disabled]="!newExerciseId || !newTeamName.trim() || busy()">
              <mat-icon>group_add</mat-icon> Create team
            </button>
          </form>
        </mat-card>
      }

      @if (teamsError()) {
        <p class="error-text" role="alert">{{ teamsError() }}</p>
      } @else if (teams() === null) {
        <div class="tn-skeleton tn-skeleton-row" aria-busy="true" aria-label="Loading teams"></div>
      } @else if (teams()!.length === 0) {
        <p class="muted">No teams yet.
          {{ myExercises().length ? 'Create one for an exercise you run above.' : 'Create teams on Users & Teams, then manage their members here.' }}</p>
        @if (outcome()) { <p class="outcome" role="status" aria-live="polite">{{ outcome() }}</p> }
      } @else {
        <div class="layout">
          <nav class="team-list" aria-label="Teams">
            <ul>
              @for (t of teams()!; track t.id) {
                <li>
                  <button type="button" class="team-btn" [class.selected]="t.id === selected()?.id"
                          [attr.aria-current]="t.id === selected()?.id ? 'true' : null" (click)="select(t)">
                    <span class="swatch" [style.background]="t.color_hex || 'var(--border)'" aria-hidden="true"></span>
                    {{ t.name }}
                  </button>
                </li>
              }
            </ul>
          </nav>

          <mat-card class="members">
            @if (selected(); as t) {
              <h2>{{ t.name }}</h2>
              <p class="muted">
                {{ members()?.length ?? '…' }} member(s){{ t.max_members ? ' of at most ' + t.max_members : '' }}
                @if (t.team_type) { · {{ t.team_type }} }
              </p>

              @if (canEdit()) {
                <form class="add" (ngSubmit)="add(t)" aria-label="Add a member">
                  <mat-form-field appearance="outline" class="grow">
                    <mat-label>Person</mat-label>
                    <mat-select name="who" [(ngModel)]="newUserId" panelClass="tn-select-panel" required>
                      @for (u of candidates(); track u.id) {
                        <mat-option [value]="u.id">{{ u.display_name }} ({{ u.email }}, {{ u.role }})</mat-option>
                      }
                    </mat-select>
                    @if (usersError()) { <mat-hint class="error-text">{{ usersError() }}</mat-hint> }
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Team role</mat-label>
                    <input matInput name="role" [(ngModel)]="newRole" maxlength="50">
                  </mat-form-field>
                  <button mat-raised-button color="primary" type="submit" [disabled]="!newUserId || busy() || full()">
                    <mat-icon>person_add</mat-icon> Add
                  </button>
                </form>
                @if (full()) { <p class="muted">The team is full ({{ t.max_members }} max).</p> }
              }

              @if (outcome()) { <p class="outcome" role="status" aria-live="polite">{{ outcome() }}</p> }

              @if (membersError()) {
                <p class="error-text" role="alert">{{ membersError() }}</p>
              } @else if (members() === null) {
                <div class="tn-skeleton tn-skeleton-row" aria-busy="true"></div>
              } @else if (members()!.length === 0) {
                <p class="muted">Nobody is on this team yet.</p>
              } @else {
                <table class="tn-table">
                  <caption class="sr-only">Members of {{ t.name }}</caption>
                  <thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Team role</th>
                    <th scope="col">Joined</th>@if (canEdit()) { <th scope="col"><span class="sr-only">Actions</span></th> }</tr></thead>
                  <tbody>
                    @for (m of members()!; track m.user_id) {
                      <tr>
                        <td>{{ m.display_name }}</td>
                        <td>{{ m.email || '—' }}</td>
                        <td>{{ m.role }}</td>
                        <td>{{ m.joined_at ? (m.joined_at | date:'mediumDate') : '—' }}</td>
                        @if (canEdit()) {
                          <td>
                            <button mat-stroked-button color="warn" (click)="remove(t, m)" [disabled]="busy()"
                                    [attr.aria-label]="'Remove ' + m.display_name + ' from ' + t.name">Remove</button>
                          </td>
                        }
                      </tr>
                    }
                  </tbody>
                </table>
              }
            } @else {
              <p class="muted">Choose a team.</p>
            }
          </mat-card>
        </div>
      }
    </div>
  `,
  styles: [`
    .layout { display: grid; grid-template-columns: minmax(180px, 240px) 1fr; gap: 16px; margin-top: 16px; }
    @media (max-width: 720px) { .layout { grid-template-columns: 1fr; } }
    .team-list ul { list-style: none; margin: 0; padding: 0; }
    .team-btn { width: 100%; text-align: left; display: flex; gap: 8px; align-items: center; padding: 10px 12px;
      border: 1px solid transparent; border-radius: var(--radius-md); background: none; color: inherit; cursor: pointer; font: inherit; }
    .team-btn:hover, .team-btn:focus-visible { border-color: var(--border); }
    .team-btn.selected { background: var(--bg-surface); border-color: var(--accent); }
    .swatch { width: 10px; height: 10px; border-radius: 50%; flex: none; }
    .members { padding: 16px; }
    .create { padding: 8px 16px 0; margin-top: 16px; }
    h2 { margin: 0; font-size: 18px; }
    .add { display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; margin-top: 12px; }
    .grow { flex: 1; min-width: 260px; }
    .muted { color: var(--text-secondary); font-size: 13px; }
    .error-text { color: var(--alert, #ef4444); }
    .outcome { border-left: 4px solid var(--accent); padding: 6px 10px; background: var(--bg-surface); }
    .tn-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
    .tn-table th, .tn-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `],
})
export class TeamMembersComponent implements OnInit {
  private api = inject(OperatorRecordsApiService);
  private auth = inject(AuthService);
  private notify = inject(NotificationService);
  private dialog = inject(MatDialog);

  teams = signal<TeamSummary[] | null>(null);
  teamsError = signal('');
  selected = signal<TeamSummary | null>(null);
  members = signal<TeamMember[] | null>(null);
  membersError = signal('');
  users = signal<DirectoryUser[]>([]);
  usersError = signal('');
  busy = signal(false);
  outcome = signal('');
  newUserId = '';
  newRole = 'member';
  newExerciseId = '';
  newTeamName = '';
  /** The caller's classes (GET /class-setup/mine); null until loaded or for an admin. */
  mine = signal<MyClasses | null>(null);

  /** user:update (admins): every team of the tenant. */
  readonly isAdmin = computed(() => roleCan(this.auth.user()?.role, 'user:update'));
  /** team:manage_own (instructors): the teams of an exercise they run. */
  readonly managesOwn = computed(() => !this.isAdmin() && roleCan(this.auth.user()?.role, 'team:manage_own'));
  readonly myExercises = computed(() => (this.managesOwn() ? this.mine()?.exercises ?? [] : []));
  /** Whether the selected team is the caller's to change. */
  readonly canEdit = computed(() => {
    if (this.isAdmin()) return true;
    const ex = this.selected()?.exercise_id;
    return !!ex && this.myExercises().some(x => x.id === ex);
  });
  readonly subtitle = computed(() => {
    if (this.isAdmin()) return '';
    if (this.managesOwn()) {
      return 'You change the teams of the exercises of your upcoming or running sessions, with the Students '
        + 'enrolled in their course; the others are read-only.';
    }
    return 'Read-only for your role.';
  });
  /** Active people not on the team; an instructor places Students only, as the API requires. */
  readonly candidates = computed(() => {
    const on = new Set((this.members() ?? []).map(m => m.user_id));
    const studentsOnly = !this.isAdmin();
    return this.users().filter(u => !on.has(u.id) && u.is_active && (!studentsOnly || u.role === 'student'));
  });
  readonly full = computed(() => {
    const max = this.selected()?.max_members;
    return !!max && (this.members()?.length ?? 0) >= max;
  });

  ngOnInit(): void {
    this.api.listTeams().subscribe({
      next: t => {
        this.teams.set(t);
        if (t.length) this.select(t[0]);
      },
      error: err => this.teamsError.set(apiError(err)),
    });
    if (this.managesOwn()) {
      this.api.myClasses().subscribe({
        next: m => this.mine.set(m),
        error: err => this.outcome.set(`Your classes could not be loaded: ${apiError(err)}`),
      });
    }
    if (this.isAdmin() || this.managesOwn()) {
      this.api.listUsers().subscribe({
        next: u => this.users.set(u),
        error: err => this.usersError.set(`People could not be listed: ${apiError(err)}`),
      });
    }
  }

  /** POST /teams for an exercise the instructor runs; the new team is selected. */
  create(): void {
    const name = this.newTeamName.trim();
    const exercise = this.myExercises().find(x => x.id === this.newExerciseId);
    if (!name || !exercise) return;
    this.busy.set(true);
    this.api.createTeam({ name, exercise_id: exercise.id, is_persistent: false }).subscribe({
      next: team => {
        this.busy.set(false);
        this.teams.set([...(this.teams() ?? []), team]);
        this.newTeamName = '';
        this.select(team);
        this.outcome.set(`Team ${team.name} created for ${exercise.name}.`);
      },
      error: err => {
        this.busy.set(false);
        const msg = apiError(err);
        this.outcome.set(`Not created: ${msg}`);
        this.notify.error(msg);
      },
    });
  }

  select(t: TeamSummary): void {
    this.selected.set(t);
    this.outcome.set('');
    this.loadMembers(t.id);
  }

  loadMembers(teamId: string): void {
    this.members.set(null);
    this.membersError.set('');
    this.api.listTeamMembers(teamId).subscribe({
      next: m => this.members.set(m),
      error: err => this.membersError.set(apiError(err)),
    });
  }

  add(t: TeamSummary): void {
    const who = this.users().find(u => u.id === this.newUserId);
    if (!who) return;
    this.busy.set(true);
    this.api.addTeamMember(t.id, who.id, this.newRole.trim() || 'member').subscribe({
      next: out => {
        this.busy.set(false);
        this.outcome.set(`${who.display_name} added to ${t.name} as ${out.role}.`);
        this.newUserId = '';
        this.loadMembers(t.id);
      },
      error: err => {
        this.busy.set(false);
        const msg = apiError(err);
        this.outcome.set(`Not added: ${msg}`);
        this.notify.error(msg);
      },
    });
  }

  remove(t: TeamSummary, m: TeamMember): void {
    const data: ImpactDialogData = {
      title: `Remove ${m.display_name} from ${t.name}?`,
      impact: [
        `${m.display_name} leaves the team.`,
        'Their account and their own records (progress, scores) are not changed; the removal is audited.',
      ],
      confirmText: 'Remove',
      danger: true,
    };
    this.dialog
      .open<ImpactDialogComponent, ImpactDialogData, ImpactDialogResult | null>(ImpactDialogComponent, {
        width: '480px', data,
      })
      .afterClosed()
      .subscribe(ok => {
        if (!ok) return;
        this.busy.set(true);
        this.api.removeTeamMember(t.id, m.user_id).subscribe({
          next: () => {
            this.busy.set(false);
            this.outcome.set(`${m.display_name} removed from ${t.name}.`);
            this.loadMembers(t.id);
          },
          error: err => {
            this.busy.set(false);
            const msg = apiError(err);
            this.outcome.set(`Not removed: ${msg}`);
            this.notify.error(msg);
            this.loadMembers(t.id);
          },
        });
      });
  }
}
