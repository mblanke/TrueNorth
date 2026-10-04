import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { Subject, of, throwError } from 'rxjs';

import { ScenarioStudioComponent } from './scenario-studio.component';
import { ApiService, YamlValidation } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Scenario } from '@core/models';

const stored: Scenario = {
  id: 's1', name: 'Phish', version: '1.2', is_public: true, tenant_id: null,
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-02T00:00:00Z',
  yaml: 'name: Phish\ntimeline:\n  - t: "00:05:00"\n    action: email\n',
};

const parsed: YamlValidation = {
  valid: true, errors: [],
  normalized: { name: 'Phish', version: '1.2', timeline: [{ t: '00:05:00', action: 'email', params: {} }] },
};

describe('ScenarioStudioComponent', () => {
  let fixture: ComponentFixture<ScenarioStudioComponent>;
  let component: ScenarioStudioComponent;
  let mockApi: jasmine.SpyObj<ApiService>;

  function setUp(validation: ReturnType<ApiService['validateScenario']>): void {
    mockApi = jasmine.createSpyObj('ApiService', [
      'listScenarios', 'listTemplates', 'listInjectors', 'getScenario',
      'validateScenario', 'updateScenario', 'createScenario',
    ]);
    mockApi.listScenarios.and.returnValue(of([]));
    mockApi.listTemplates.and.returnValue(of([]));
    mockApi.listInjectors.and.returnValue(of([]));
    mockApi.getScenario.and.returnValue(of(stored));
    mockApi.validateScenario.and.returnValue(validation);
    mockApi.updateScenario.and.returnValue(of(stored));
    mockApi.createScenario.and.returnValue(of(stored));

    TestBed.configureTestingModule({
      imports: [ScenarioStudioComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: MatDialog, useValue: { open: jasmine.createSpy('open') } },
      ],
    });
    fixture = TestBed.createComponent(ScenarioStudioComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  function saveButton(): HTMLButtonElement {
    return Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>)
      .find(b => b.textContent?.includes('Save')) as HTMLButtonElement;
  }

  it('should keep a public scenario public when it is saved', () => {
    setUp(of(parsed));
    component['open']('s1');
    fixture.detectChanges();

    component['save']();

    const body = mockApi.updateScenario.calls.mostRecent().args[1];
    expect(body.is_public).toBeTrue();
  });

  it('should save the visibility the author toggles', () => {
    setUp(of(parsed));
    component['open']('s1');
    component['isPublic'].set(false);

    component['save']();

    expect(mockApi.updateScenario.calls.mostRecent().args[1].is_public).toBeFalse();
  });

  it('should start a new scenario private', () => {
    setUp(of(parsed));
    component['open']('s1');
    component['newScenario']();
    component['setField']('name', 'Fresh');

    component['save']();

    expect(mockApi.createScenario.calls.mostRecent().args[0].is_public).toBeFalse();
  });

  it('should refuse to save over a scenario whose YAML could not be loaded', () => {
    setUp(throwError(() => new Error('validator down')));
    component['open']('s1');
    fixture.detectChanges();

    expect(saveButton().disabled).toBeTrue();
    expect(fixture.nativeElement.textContent).toContain('Read-only');
    component['save']();
    expect(mockApi.updateScenario).not.toHaveBeenCalled();
  });

  it('should open a scenario with keys the editor cannot keep as read-only', () => {
    setUp(of({ ...parsed, normalized: { ...parsed.normalized, mitre_attack: ['T1566'] } }));
    component['open']('s1');
    fixture.detectChanges();

    expect(saveButton().disabled).toBeTrue();
    expect(fixture.nativeElement.textContent).toContain('mitre_attack');
    component['save']();
    expect(mockApi.updateScenario).not.toHaveBeenCalled();
  });

  it('should change only visibility from a read-only scenario, never its YAML', () => {
    setUp(of({ ...parsed, normalized: { ...parsed.normalized, scoring: { total: 100 } } }));
    component['open']('s1');

    component['setPublic'](false);

    expect(mockApi.updateScenario).toHaveBeenCalledOnceWith('s1', { is_public: false });
  });

  it('should not send anything when toggling visibility on an editable scenario', () => {
    setUp(of(parsed));
    component['open']('s1');

    component['setPublic'](false);

    expect(mockApi.updateScenario).not.toHaveBeenCalled();
    expect(component['isPublic']()).toBeFalse();
  });

  it('should ignore an Open that is overtaken by a later one', () => {
    setUp(of(parsed));
    const slow = new Subject<Scenario>();
    const other: Scenario = { ...stored, id: 's2', name: 'Other', is_public: false };
    mockApi.getScenario.and.callFake((id: string) => (id === 's1' ? slow : of(other)));

    component['open']('s1');
    component['open']('s2');
    slow.next(stored);

    expect(component['editingId']()).toBe('s2');
    expect(component['isPublic']()).toBeFalse();
  });

  it('should refuse to save when the stored YAML does not parse', () => {
    setUp(of({ valid: false, errors: [{ path: '', message: 'bad yaml' }], normalized: null }));
    component['open']('s1');
    fixture.detectChanges();

    component['save']();

    expect(saveButton().disabled).toBeTrue();
    expect(mockApi.updateScenario).not.toHaveBeenCalled();
  });
});
