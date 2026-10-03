import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';

import { ContentCatalogComponent, countTemplateHosts } from './content-catalog.component';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { Template } from '@core/models';

describe('ContentCatalogComponent', () => {
  let fixture: ComponentFixture<ContentCatalogComponent>;
  let component: ContentCatalogComponent;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockNotify: jasmine.SpyObj<NotificationService>;
  let dialogOpen: jasmine.Spy;

  const templates: Partial<Template>[] = [
    {
      id: 't1', name: 'Small Enterprise', version: '1.0', is_public: true,
      yaml: 'name: small\nassets:\n  - role: domain_controller\n  - role: workstation\n    count: 3\n',
    },
    {
      id: 't2', name: 'Medium Enterprise', version: '2.1', is_public: false,
      yaml: 'name: medium\nnodes:\n  - label: dc01\n  - label: srv01\n',
    },
  ];

  /** Default: dialogs resolve to true (confirm accepted). */
  function setUp(list: Partial<Template>[], afterClosed: unknown = true): void {
    mockApi = jasmine.createSpyObj('ApiService', [
      'listTemplates', 'createTemplate', 'updateTemplate', 'deleteTemplate',
      'validateTemplate', 'templateDiagramPreview',
    ]);
    mockNotify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);

    mockApi.listTemplates.and.returnValue(of(list as Template[]));
    mockApi.createTemplate.and.returnValue(of(list[0] as Template));
    mockApi.updateTemplate.and.returnValue(of(list[0] as Template));
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

    component.confirmDelete(templates[0] as Template);

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

    component.confirmDelete(templates[0] as Template);

    expect(dialogOpen).toHaveBeenCalled();
    expect(mockApi.deleteTemplate).not.toHaveBeenCalled();
  });

  // ── Validate from a card ─────────────────────────────────────────
  it('should validate a template and record the verdict', () => {
    setUp(templates);
    fixture.detectChanges();

    component.validate(templates[0] as Template);

    expect(mockApi.validateTemplate).toHaveBeenCalledWith(templates[0].yaml as string);
    expect(component.verdict('t1')).toEqual({ valid: true, problems: 0 });
    expect(mockNotify.success).toHaveBeenCalledWith('Template is valid');
  });

  it('should surface validation problems', () => {
    setUp(templates);
    mockApi.validateTemplate.and.returnValue(
      of({ valid: false, errors: [{ path: 'assets[0].role', message: 'unknown role' }], normalized: null }),
    );
    fixture.detectChanges();

    component.validate(templates[0] as Template);

    expect(component.verdict('t1')).toEqual({ valid: false, problems: 1 });
    expect(mockNotify.error).toHaveBeenCalled();
  });

  // ── Host count derived from YAML ─────────────────────────────────
  it('should derive host counts from the template YAML', () => {
    expect(countTemplateHosts(templates[0].yaml)).toBe(4);
    expect(countTemplateHosts(templates[1].yaml)).toBe(2);
    expect(countTemplateHosts('name: bare\n')).toBeNull();
    expect(countTemplateHosts('')).toBeNull();
  });
});
