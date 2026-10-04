import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { MAT_DIALOG_DATA, MatDialog, MatDialogRef } from '@angular/material/dialog';
import { NEVER, Observable, Subject, of, throwError } from 'rxjs';

import { ContentCatalogComponent, TemplateEditorDialogComponent } from './content-catalog.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Template, TemplateSummary } from '@core/models';

/** Real GET /templates rows: host_count but no yaml, tenant_id or updated_at. */
const templates: TemplateSummary[] = [
  {
    id: 't1', name: 'Small Enterprise', version: '1.0', is_public: true,
    created_at: '2026-10-01T00:00:00Z', host_count: 4,
  },
  {
    id: 't2', name: 'Medium Enterprise', version: '2.1', is_public: false,
    created_at: '2026-10-02T00:00:00Z', host_count: null,
  },
];

/** GET /templates/{id}: the full body, yaml included. */
const fullTemplates: Record<string, Template> = {
  t1: {
    id: 't1', name: 'Small Enterprise', version: '1.0', is_public: true,
    created_at: '2026-10-01T00:00:00Z', tenant_id: null, updated_at: '2026-10-01T00:00:00Z',
    yaml: 'name: small\nassets:\n  - role: domain_controller\n  - role: workstation\n    count: 3\n',
  },
};

describe('ContentCatalogComponent', () => {
  let fixture: ComponentFixture<ContentCatalogComponent>;
  let component: ContentCatalogComponent;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;
  let dialogOpen: jasmine.Spy;

  /** Default: dialogs resolve to true (confirm accepted). */
  function setUp(list: TemplateSummary[], afterClosed: unknown = true): void {
    mockApi = jasmine.createSpyObj('ApiService', [
      'listTemplates', 'getTemplate', 'createTemplate', 'updateTemplate', 'deleteTemplate',
      'validateTemplate', 'templateDiagramPreview',
    ]);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);

    mockApi.listTemplates.and.returnValue(of(list));
    mockApi.getTemplate.and.callFake((id: string) => of(fullTemplates[id]));
    mockApi.createTemplate.and.returnValue(of(fullTemplates['t1']));
    mockApi.updateTemplate.and.returnValue(of(fullTemplates['t1']));
    mockApi.deleteTemplate.and.returnValue(of(undefined as void));
    mockApi.validateTemplate.and.returnValue(of({ valid: true, errors: [], normalized: {} }));
    mockApi.templateDiagramPreview.and.returnValue(of({ template_id: 't1', diagram_json: {} }));

    dialogOpen = jasmine.createSpy('open').and.returnValue({
      afterClosed: () => of(afterClosed),
    });

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ContentCatalogComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: mockApi },
        { provide: NotificationService, useValue: mockNotify },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    });

    fixture = TestBed.createComponent(ContentCatalogComponent);
    component = fixture.componentInstance;
  }

  // ── Creation / load ──────────────────────────────────────────────
  it('should create and load templates on init', () => {
    setUp(templates);
    fixture.detectChanges();

    expect(component).toBeTruthy();
    expect(mockApi.listTemplates).toHaveBeenCalled();
    expect(component.templates().length).toBe(2);
    expect(component.loading()).toBeFalse();
  });

  // ── One card per template ────────────────────────────────────────
  it('should render one card per template', () => {
    setUp(templates);
    fixture.detectChanges();

    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('mat-card').length).toBe(2);
    expect(el.textContent).toContain('Small Enterprise');
    expect(el.textContent).toContain('Medium Enterprise');
  });

  // ── Use Template link carries the template id ────────────────────
  it('should link Use Template at the range designer with the template id', () => {
    setUp(templates);
    fixture.detectChanges();

    const link = fixture.nativeElement.querySelector('a[href*="designer"]') as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.getAttribute('href')).toContain('template=t1');
  });

  // ── Empty state ──────────────────────────────────────────────────
  it('should render the empty state when there are no templates', () => {
    setUp([]);
    fixture.detectChanges();

    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('tn-empty-state')).toBeTruthy();
    expect(el.querySelectorAll('mat-card').length).toBe(0);
  });

  // ── Delete: confirmed ────────────────────────────────────────────
  it('should open the confirm dialog and delete when confirmed', () => {
    setUp(templates, true);
    fixture.detectChanges();

    component.confirmDelete(templates[0]);

    expect(dialogOpen).toHaveBeenCalled();
    expect(mockApi.deleteTemplate).toHaveBeenCalledWith('t1');
    expect(mockNotify.success).toHaveBeenCalledWith('Template deleted');
    // reloaded after the delete
    expect(mockApi.listTemplates).toHaveBeenCalledTimes(2);
  });

  // ── Delete: cancelled ────────────────────────────────────────────
  it('should not delete when the confirm dialog is dismissed', () => {
    setUp(templates, false);
    fixture.detectChanges();

    component.confirmDelete(templates[0]);

    expect(dialogOpen).toHaveBeenCalled();
    expect(mockApi.deleteTemplate).not.toHaveBeenCalled();
  });

  // ── Host badge: straight off the list row, no per-card fetch ─────
  it('should show the host count from the list row and nothing when it is null', () => {
    setUp(templates);
    fixture.detectChanges();

    expect(mockApi.getTemplate).not.toHaveBeenCalled();
    const badges = fixture.nativeElement.querySelectorAll('.hosts');
    expect(badges.length).toBe(1);
    expect((badges[0] as HTMLElement).textContent?.trim()).toMatch(/4 hosts$/);
  });

  // ── Validate from a card ─────────────────────────────────────────
  it('should validate the full template YAML, not the empty list row', () => {
    setUp(templates);
    fixture.detectChanges();

    component.validate(templates[0]);

    expect(mockApi.getTemplate).toHaveBeenCalledWith('t1');
    expect(mockApi.validateTemplate).toHaveBeenCalledWith(fullTemplates['t1'].yaml);
    expect(component.verdict('t1')).toEqual({ valid: true, problems: 0 });
    expect(mockNotify.success).toHaveBeenCalledWith('Template is valid');
  });

  it('should surface validation problems', () => {
    setUp(templates);
    mockApi.validateTemplate.and.returnValue(
      of({ valid: false, errors: [{ path: 'assets[0].role', message: 'unknown role' }], normalized: null }),
    );
    fixture.detectChanges();

    component.validate(templates[0]);

    expect(component.verdict('t1')).toEqual({ valid: false, problems: 1 });
    expect(mockNotify.error).toHaveBeenCalled();
  });

  it('should not validate when the full template cannot be loaded', () => {
    setUp(templates);
    fixture.detectChanges();
    mockApi.getTemplate.and.returnValue(throwError(() => new Error('boom')));

    component.validate(templates[0]);

    expect(mockApi.validateTemplate).not.toHaveBeenCalled();
    expect(mockNotify.error).toHaveBeenCalledWith('Validation failed');
  });

  // ── Edit: saves what the dialog returns ──────────────────────────
  it('should PUT the edited template the dialog closes with', () => {
    const edited = { name: 'Small', version: '1.1', yaml: fullTemplates['t1'].yaml, is_public: true };
    setUp(templates, edited);
    fixture.detectChanges();

    component.openEditor(templates[0]);

    expect(dialogOpen.calls.mostRecent().args[1].data).toEqual({ template: templates[0] });
    expect(mockApi.updateTemplate).toHaveBeenCalledWith('t1', edited);
  });
});

describe('TemplateEditorDialogComponent', () => {
  let mockApi: jasmine.SpyObj<ApiService>;
  let close: jasmine.Spy;

  /** The dialog fetches in its constructor, so `body` is primed before it is created. */
  function create(
    template: TemplateSummary | null, body: Observable<Template> = NEVER,
  ): ComponentFixture<TemplateEditorDialogComponent> {
    mockApi = jasmine.createSpyObj('ApiService', ['getTemplate', 'validateTemplate']);
    mockApi.getTemplate.and.returnValue(body);
    close = jasmine.createSpy('close');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [TemplateEditorDialogComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: mockApi },
        { provide: MatDialogRef, useValue: { close } },
        { provide: MAT_DIALOG_DATA, useValue: { template } },
      ],
    });
    return TestBed.createComponent(TemplateEditorDialogComponent);
  }

  function saveButton(f: ComponentFixture<TemplateEditorDialogComponent>): HTMLButtonElement {
    const buttons = f.nativeElement.querySelectorAll('mat-dialog-actions button');
    return buttons[buttons.length - 1] as HTMLButtonElement;
  }

  it('should keep Save disabled until the full template has loaded', () => {
    const body = new Subject<Template>();
    const f = create(templates[0], body);
    f.detectChanges();

    expect(mockApi.getTemplate).toHaveBeenCalledWith('t1');
    expect(saveButton(f).disabled).toBeTrue();
    f.componentInstance.save();
    expect(close).not.toHaveBeenCalled();

    body.next(fullTemplates['t1']);
    f.detectChanges();

    expect(f.componentInstance.form.yaml).toBe(fullTemplates['t1'].yaml);
    expect(saveButton(f).disabled).toBeFalse();
    f.componentInstance.save();
    expect(close).toHaveBeenCalledWith({
      name: 'Small Enterprise', version: '1.0', yaml: fullTemplates['t1'].yaml, is_public: true,
    });
  });

  it('should never save when the full template fails to load', () => {
    const f = create(templates[0], throwError(() => new Error('boom')));
    f.detectChanges();

    expect(f.componentInstance.load()).toBe('failed');
    expect(saveButton(f).disabled).toBeTrue();
    f.componentInstance.save();
    expect(close).not.toHaveBeenCalled();
  });

  it('should not save a template with an empty YAML body', () => {
    const f = create(null);
    f.detectChanges();
    f.componentInstance.form.name = 'New';
    f.detectChanges();

    expect(mockApi.getTemplate).not.toHaveBeenCalled();
    expect(f.componentInstance.canSave()).toBeFalse();
    f.componentInstance.save();
    expect(close).not.toHaveBeenCalled();
  });
});
