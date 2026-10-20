import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { OperatorRecordsApiService } from '@core/services/operator-records-api.service';
import { TeamMembersComponent } from './team-members.component';

const TEAMS = [
  { id: 't1', name: 'Blue Cell', max_members: 2, is_persistent: true, created_at: '', updated_at: '' },
  { id: 't2', name: 'Red Cell', max_members: null, is_persistent: true, created_at: '', updated_at: '' },
];
/** A team of exercise x1, which the instructor runs (GET /class-setup/mine), first in the list. */
const EX_TEAM = { id: 't3', name: 'Exercise Blue', max_members: null, is_persistent: false, exercise_id: 'x1',
  created_at: '', updated_at: '' };
const MINE = { courses: [], exercises: [{ id: 'x1', name: 'Op Northwind' }] };
const MEMBERS = [{ user_id: 'u1', display_name: 'Alice', email: 'a@x.test', role: 'lead', joined_at: '2026-10-01T00:00:00Z' }];
const USERS = [
  { id: 'u1', display_name: 'Alice', email: 'a@x.test', role: 'student', is_active: true },
  { id: 'u2', display_name: 'Bob', email: 'b@x.test', role: 'student', is_active: true },
  { id: 'u3', display_name: 'Gone', email: 'g@x.test', role: 'student', is_active: false },
];

describe('TeamMembersComponent', () => {
  let fixture: ComponentFixture<TeamMembersComponent>;
  let component: TeamMembersComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<OperatorRecordsApiService>;
  let dialogOpen: jasmine.Spy;

  async function render(role: UserRole, teams: unknown = of(TEAMS), mine: unknown = of(MINE)) {
    api = jasmine.createSpyObj('OperatorRecordsApiService', ['listTeams', 'listTeamMembers', 'addTeamMember',
      'removeTeamMember', 'listUsers', 'myClasses', 'createTeam']);
    api.listTeams.and.returnValue(teams as any);
    api.myClasses.and.returnValue(mine as any);
    api.listTeamMembers.and.returnValue(of(MEMBERS));
    api.listUsers.and.returnValue(of(USERS as any));
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of({ reason: '' }) }));
    await TestBed.configureTestingModule({
      imports: [TeamMembersComponent, NoopAnimationsModule],
      providers: [
        { provide: OperatorRecordsApiService, useValue: api },
        { provide: AuthService, useValue: { user: signal({ id: 'me', role }) } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(TeamMembersComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  it('lists teams and the first team\'s members', async () => {
    await render('admin');
    expect(el.textContent).toContain('Blue Cell');
    expect(el.textContent).toContain('Red Cell');
    expect(api.listTeamMembers).toHaveBeenCalledWith('t1');
    expect(el.textContent).toContain('Alice');
    expect(el.textContent).toContain('1 member(s) of at most 2');
  });

  it('switches team from the keyboard-reachable list', async () => {
    await render('admin');
    const red = Array.from(el.querySelectorAll('.team-btn')).find(b => b.textContent?.includes('Red')) as HTMLButtonElement;
    expect(red.tagName).toBe('BUTTON');
    red.click();
    fixture.detectChanges();
    expect(api.listTeamMembers).toHaveBeenCalledWith('t2');
    expect(red.getAttribute('aria-current')).toBe('true');
  });

  it('an instructor reads a team of an exercise they do not run, with no add or remove', async () => {
    await render('instructor');
    expect(el.textContent).toContain('the others are read-only');
    expect(el.textContent).not.toContain('Read-only for your role.');
    expect(component.selected()?.id).toBe('t1');
    expect(el.querySelector('form[aria-label="Add a member"]')).toBeNull();
    expect(el.textContent).not.toContain('Remove');
  });

  it('an instructor adds Students to a team of an exercise they run (#200)', async () => {
    await render('instructor', of([EX_TEAM, ...TEAMS]));
    expect(component.selected()?.id).toBe('t3');
    expect(el.querySelector('form[aria-label="Add a member"]')).not.toBeNull();
    expect(el.textContent).toContain('Remove');
    // Students only: the API refuses staff for team:manage_own.
    const withStaff = [...USERS, { id: 'i9', display_name: 'Ivy', email: 'i@x.test', role: 'instructor', is_active: true }];
    component.users.set(withStaff as any);
    expect(component.candidates().map(u => u.id)).toEqual(['u2']);
  });

  it('an instructor creates a team for an exercise they run and it is selected (#200)', async () => {
    await render('instructor');
    const form = el.querySelector('form[aria-label="New team for an exercise you run"]');
    expect(form).not.toBeNull();
    api.createTeam.and.returnValue(of({ ...EX_TEAM, id: 't9', name: 'Red Team' } as any));
    component.newExerciseId = 'x1';
    component.newTeamName = 'Red Team';
    component.create();
    fixture.detectChanges();
    expect(api.createTeam).toHaveBeenCalledWith({ name: 'Red Team', exercise_id: 'x1', is_persistent: false });
    expect(component.selected()?.id).toBe('t9');
    expect(el.textContent).toContain('Team Red Team created for Op Northwind.');
    expect(el.querySelector('form[aria-label="Add a member"]')).not.toBeNull();
  });

  it('shows the API refusal when creating a team', async () => {
    await render('instructor');
    api.createTeam.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 403, error: { detail: 'You can create teams only for an exercise you run' } })));
    component.newExerciseId = 'x1';
    component.newTeamName = 'X';
    component.create();
    fixture.detectChanges();
    expect(el.textContent).toContain('Not created: You can create teams only for an exercise you run');
  });

  it('an instructor who runs no exercise gets no create form', async () => {
    await render('instructor', of(TEAMS), of({ courses: [], exercises: [] }));
    expect(el.querySelector('form[aria-label="New team for an exercise you run"]')).toBeNull();
  });

  it('an observer reads only', async () => {
    await render('observer');
    expect(el.textContent).toContain('Read-only for your role.');
    expect(el.querySelector('form')).toBeNull();
    expect(api.listUsers).not.toHaveBeenCalled();
    expect(api.myClasses).not.toHaveBeenCalled();
  });

  it('offers only active people not already on the team', async () => {
    await render('admin');
    expect(component.candidates().map(u => u.id)).toEqual(['u2']);
  });

  it('adds a member and shows the API outcome', async () => {
    await render('admin');
    api.addTeamMember.and.returnValue(of({ user_id: 'u2', team_id: 't1', role: 'member' }));
    component.newUserId = 'u2';
    component.add(TEAMS[0] as any);
    fixture.detectChanges();
    expect(api.addTeamMember).toHaveBeenCalledWith('t1', 'u2', 'member');
    expect(el.textContent).toContain('Bob added to Blue Cell as member.');
    expect(api.listTeamMembers).toHaveBeenCalledTimes(2);
  });

  it('shows the API refusal when the team is full', async () => {
    await render('admin');
    api.addTeamMember.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: 'Team is full (2 max)' } })));
    component.newUserId = 'u2';
    component.add(TEAMS[0] as any);
    fixture.detectChanges();
    expect(el.textContent).toContain('Not added: Team is full (2 max)');
  });

  it('removes only after the impact dialog', async () => {
    await render('admin');
    api.removeTeamMember.and.returnValue(of(undefined));
    (Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Remove')) as HTMLButtonElement).click();
    expect(dialogOpen).toHaveBeenCalled();
    expect(api.removeTeamMember).toHaveBeenCalledWith('t1', 'u1');
    fixture.detectChanges();
    expect(el.textContent).toContain('Alice removed from Blue Cell.');
  });

  it('shows a load error rather than "no teams"', async () => {
    await render('admin', throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'Missing permission: user:read' } })));
    expect(el.textContent).toContain('Missing permission: user:read');
    expect(el.textContent).not.toContain('No teams yet');
  });
});
