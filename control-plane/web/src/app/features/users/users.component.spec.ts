import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { DirectoryApiService } from '@core/services/directory-api.service';
import { RegistrationApiService } from '@core/services/registration-api.service';
import { UserFull } from '@core/models';
import { UsersComponent } from './users.component';
import { GROUP_TYPES, OU_TYPES } from './directory-types';

describe('UsersComponent', () => {
  let fixture: ComponentFixture<UsersComponent>;
  let component: UsersComponent;
  let api: jasmine.SpyObj<ApiService>;
  let snack: jasmine.Spy;

  const people = [
    { id: 'u1', email: 'ada@truenorth.local', display_name: 'Ada Lovelace', role: 'student', callsign: 'COUNT' },
    { id: 'u2', email: 'grace@truenorth.local', display_name: 'Grace Hopper', role: 'instructor', callsign: 'AMAZING' },
    { id: 'u3', email: 'alan@truenorth.local', display_name: 'Alan Turing', role: 'student', is_active: false },
  ] as unknown as UserFull[];

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['listUsers', 'listTeams', 'deleteUser', 'setUserActive']);
    api.listUsers.and.returnValue(of(people) as unknown as ReturnType<ApiService['listUsers']>);
    api.listTeams.and.returnValue(of([]));
    api.deleteUser.and.returnValue(of(undefined));
    api.setUserActive.and.returnValue(of(people[2]) as unknown as ReturnType<ApiService['setUserActive']>);

    const directory = jasmine.createSpyObj('DirectoryApiService', [
      'nations', 'coalitions', 'ouTree', 'securityGroups', 'adSyncStatus', 'authZones', 'adStatusSync',
    ]);
    for (const m of ['nations', 'coalitions', 'ouTree', 'securityGroups', 'authZones']) {
      directory[m].and.returnValue(of([]));
    }
    directory.adSyncStatus.and.returnValue(throwError(() => new Error('not configured')));
    directory.adStatusSync.and.returnValue(throwError(() => new Error('not configured')));
    // The Approvals tab is the default-rendered panel's sibling; keep it off the network.
    const registration = jasmine.createSpyObj('RegistrationApiService', ['listRequests']);
    registration.listRequests.and.returnValue(of([]));

    await TestBed.configureTestingModule({
      imports: [UsersComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: DirectoryApiService, useValue: directory },
        { provide: RegistrationApiService, useValue: registration },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UsersComponent);
    component = fixture.componentInstance;
    // MatSnackBarModule in the component's imports provides its own MatSnackBar, so spy
    // on the instance the component actually receives.
    snack = spyOn(fixture.debugElement.injector.get(MatSnackBar), 'open');
    fixture.detectChanges();
  });

  it('creates and loads the personnel list', () => {
    expect(component).toBeTruthy();
    expect(api.listUsers).toHaveBeenCalled();
    expect(component.users.length).toBe(3);
    // A directory endpoint that fails leaves its section empty, not the page broken.
    expect(component.adSyncStatus).toBeNull();
  });

  it('filters by name, email or callsign', () => {
    component.userSearch = 'amaz';
    expect(component.filteredUsers.map(u => u.id)).toEqual(['u2']);
    component.userSearch = 'ADA@';
    expect(component.filteredUsers.map(u => u.id)).toEqual(['u1']);
  });

  it('deletes only after confirmation', () => {
    const confirm = spyOn(window, 'confirm').and.returnValue(false);
    component.deleteUser(people[0]);
    expect(api.deleteUser).not.toHaveBeenCalled();

    confirm.and.returnValue(true);
    api.listUsers.calls.reset();
    component.deleteUser(people[0]);
    expect(api.deleteUser).toHaveBeenCalledWith('u1');
    expect(api.listUsers).toHaveBeenCalled();
    expect(snack).toHaveBeenCalledWith('User deleted', '', jasmine.any(Object));
  });

  it('offers Re-enable, not Delete, for a deactivated account', () => {
    const rows = Array.from(fixture.nativeElement.querySelectorAll('tr.mat-mdc-row')) as HTMLElement[];
    const alan = rows.find(r => r.textContent?.includes('Alan Turing'))!;
    const ada = rows.find(r => r.textContent?.includes('Ada Lovelace'))!;
    expect(alan.textContent).toContain('Deactivated');
    expect(alan.querySelector('[aria-label="Re-enable"]')).not.toBeNull();
    expect(alan.querySelector('[aria-label="Delete"]')).toBeNull();
    expect(ada.querySelector('[aria-label="Re-enable"]')).toBeNull();
    expect(ada.querySelector('[aria-label="Delete"]')).not.toBeNull();
  });

  it('re-enables through PATCH ?is_active=true and reloads', () => {
    api.listUsers.calls.reset();
    component.reEnableUser(people[2]);
    expect(api.setUserActive).toHaveBeenCalledWith('u3', true);
    expect(api.listUsers).toHaveBeenCalled();
    expect(snack).toHaveBeenCalledWith('Alan Turing re-enabled', '', jasmine.any(Object));
  });

  it("shows the API's reason when re-enabling is refused", () => {
    api.setUserActive.and.returnValue(throwError(() => ({ error: { detail: 'User not found' } })));
    component.reEnableUser(people[2]);
    expect(snack).toHaveBeenCalledWith('User not found', 'OK', jasmine.any(Object));
  });

  // The API's enums (schemas.py OUIn / SecurityGroupIn): "department", "team" and "role" were refused.
  it('offers only the OU and group types the API accepts, and defaults to one of them', () => {
    const ouTypes = ['country', 'branch', 'division', 'unit', 'section', 'custom'];
    const groupTypes = ['access', 'distribution', 'training', 'role_based'];
    expect(OU_TYPES.map(t => t.value)).toEqual(ouTypes);
    expect(GROUP_TYPES.map(t => t.value)).toEqual(groupTypes);
    expect(ouTypes).toContain(component.newOU.ou_type);
    expect(groupTypes).toContain(component.newGroup.group_type);
    component.cancelOuEdit();
    component.cancelGroupEdit();
    expect(ouTypes).toContain(component.newOU.ou_type);
    expect(groupTypes).toContain(component.newGroup.group_type);
  });
});
