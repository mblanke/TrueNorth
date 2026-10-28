import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { ContentPortApiService } from '@core/services/content-port-api.service';
import { ExerciseTemplate, ExerciseTemplatesApiService } from '@core/services/exercise-templates-api.service';
import { NotificationService } from '@core/services/notification.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { ExerciseTemplateEditorComponent } from './exercise-template-editor.component';

const TEMPLATE = {
  id: 't1', name: 'IR drill', description: 'd', revision: 3, tenant_id: 'x', is_public: false, shared: false,
  scenario_id: null, range_template_id: null, instructor_notes: 'hold', tags: ['DFIR'],
  teams: [{ name: 'Blue', side: 'blue', min_students: 2, max_students: 6 }],
  roles: [{ name: 'Analyst', description: '', team: 'Blue', min_participants: 2, max_participants: 12, default_count: 4,
    vm_profile: [{ node_type: 'ubuntu-2404', count: 1 }] }],
  scaling_rules: [{ name: 'Targets', node_type: 'srv2022', kind: 'per_participants', count: 1, per: 4, role: 'Analyst', segment: '' }],
  objectives: [{ ref: 'O1', text: 'Detect', moe: '', competency_code: '' }],
  mesl: [{ serial: 1, phase: '', scenario_time: 'D1 0900', title: 'Phish', description: '', objective_ref: 'O1',
    attack_technique: '', delivery_method: 'email', from_cell: '', to_participant: '', expected_action: '', moe: '' }],
  created_at: '', updated_at: '',
} as unknown as ExerciseTemplate;

describe('ExerciseTemplateEditorComponent', () => {
  let api: jasmine.SpyObj<ExerciseTemplatesApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  async function setup(id: string, template: ExerciseTemplate = TEMPLATE) {
    api = jasmine.createSpyObj('ExerciseTemplatesApiService', ['get', 'create', 'update', 'remove', 'exercises']);
    api.get.and.returnValue(of(template));
    api.exercises.and.returnValue(of([]));
    api.update.and.returnValue(of({ ...template, revision: 4 } as ExerciseTemplate));
    api.create.and.returnValue(of({ ...template, id: 'new1' } as ExerciseTemplate));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);
    const apiService = jasmine.createSpyObj('ApiService', ['listScenarios', 'listTemplates', 'listRanges']);
    apiService.listScenarios.and.returnValue(of([]));
    apiService.listTemplates.and.returnValue(of([]));
    apiService.listRanges.and.returnValue(of([]));
    const port = jasmine.createSpyObj('ContentPortApiService', ['proven']);
    port.proven.and.returnValue(of({ proven: false, label: 'Not proven: x', conditions: [], dependencies: [], golden_builds: [] }));
    await TestBed.configureTestingModule({
      imports: [ExerciseTemplateEditorComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id }) } } },
        { provide: ExerciseTemplatesApiService, useValue: api },
        { provide: ApiService, useValue: apiService },
        { provide: ContentPortApiService, useValue: port },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: fakeAuth('instructor') },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(ExerciseTemplateEditorComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('loads a template into the form and shows the Proven chip', async () => {
    const f = await setup('t1');
    const c = f.componentInstance;
    expect(c.name).toBe('IR drill');
    expect(c.teams().length).toBe(1);
    expect(c.objectives()[0].ref).toBe('O1');
    expect(c.mesl()[0].delivery_method).toBe('email');
    expect(f.nativeElement.querySelector('tn-proven-chip')).not.toBeNull();
  });

  it('loads roles with their VM profile and the scaling rules, and sends them back', async () => {
    const f = await setup('t1');
    const c = f.componentInstance;
    expect(c.roles()[0].vm_profile[0].node_type).toBe('ubuntu-2404');
    expect(c.rules()[0].per).toBe(4);
    c.addRole();
    c.addVm(c.roles()[1]);
    c.addRule();
    c.save();
    const sent = api.update.calls.mostRecent().args[1];
    expect(sent.roles!.length).toBe(2);
    expect(sent.roles![1].vm_profile!.length).toBe(1);
    // a fixed rule carries no step size or role
    expect(sent.scaling_rules![1]).toEqual(jasmine.objectContaining({ kind: 'fixed', per: 0, role: '' }));
    expect(sent.scaling_rules![0]).toEqual(jasmine.objectContaining({ per: 4, role: 'Analyst' }));
  });

  it('saves changes with an update and shows the new revision', async () => {
    const f = await setup('t1');
    const c = f.componentInstance;
    c.name = 'Renamed';
    c.save();
    expect(api.update).toHaveBeenCalled();
    expect(api.update.calls.mostRecent().args[1].name).toBe('Renamed');
    expect(c.revision()).toBe(4);
    expect(notify.success).toHaveBeenCalled();
  });

  it('creates a new template and opens it', async () => {
    const f = await setup('new');
    const router = TestBed.inject(Router);
    const nav = spyOn(router, 'navigate').and.resolveTo(true);
    const c = f.componentInstance;
    c.name = 'Fresh';
    c.addTeam();
    c.addObjective();
    c.addSerial();
    expect(c.mesl()[0].serial).toBe(1);
    c.save();
    expect(api.create).toHaveBeenCalled();
    expect(nav).toHaveBeenCalledWith(['/exercises/templates', 'new1']);
  });

  it('shows the API reason when a save is refused', async () => {
    const f = await setup('t1');
    api.update.and.returnValue(throwError(() => ({ error: { detail: 'objectives: a objective is listed twice' } })));
    f.componentInstance.save();
    expect(f.componentInstance.error()).toContain('listed twice');
  });

  it('is read-only for a template shared by another tenant', async () => {
    const f = await setup('t1', { ...TEMPLATE, shared: true } as ExerciseTemplate);
    expect(f.componentInstance.readOnly()).toBeTrue();
    f.componentInstance.save();
    expect(api.update).not.toHaveBeenCalled();
  });
});
