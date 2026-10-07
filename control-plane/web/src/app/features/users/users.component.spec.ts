import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { DirectoryApiService } from '@core/services/directory-api.service';
import { RegistrationApiService } from '@core/services/registration-api.service';
import { UserFull } from '@core/models';
import { UsersComponent } from './users.component';

describe('UsersComponent', () => {
  let fixture: ComponentFixture<UsersComponent>;
  let component: UsersComponent;
  let api: jasmine.SpyObj<ApiService>;
  let snack: jasmine.Spy;

  const people = [
    { id: 'u1', email: 'ada@truenorth.local', display_name: 'Ada Lovelace', role: 'student', callsign: 'COUNT' },
    { id: 'u2', email: 'grace@truenorth.local', display_name: 'Grace Hopper', role: 'instructor', callsign: 'AMAZING' },
  ] as unknown as UserFull[];

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['listUsers', 'listTeams', 'deleteUser']);
    api.listUsers.and.returnValue(of(people) as unknown as ReturnType<ApiService['listUsers']>);
    api.listTeams.and.returnValue(of([]));
    api.deleteUser.and.returnValue(of(undefined));

    const directory = jasmine.createSpyObj('DirectoryApiService', [
      'nations', 'coalitions', 'ouTree', 'securityGroups', 'adSyncStatus', 'authZones',
    ]);
    for (const m of ['nations', 'coalitions', 'ouTree', 'securityGroups', 'authZones']) {
      directory[m].and.returnValue(of([]));
    }
    directory.adSyncStatus.and.returnValue(throwError(() => new Error('not configured')));
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
    expect(component.users.length).toBe(2);
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
});
