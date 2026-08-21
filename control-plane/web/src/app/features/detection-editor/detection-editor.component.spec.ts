import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpClient } from '@angular/common/http';
import { MatChipInputEvent } from '@angular/material/chips';
import { of, throwError } from 'rxjs';
import { DetectionEditorComponent } from './detection-editor.component';
import { ApiService } from '@core/services/api.service';

describe('DetectionEditorComponent', () => {
  let component: DetectionEditorComponent;
  let fixture: ComponentFixture<DetectionEditorComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;
  let mockHttp: jasmine.SpyObj<HttpClient>;

  const rule = {
    id: 'r-1',
    title: 'LSASS access',
    sigma_id: 'tn-detect-lsass',
    status: 'stable',
    description: 'Detects LSASS handle access',
    author: 'TrueNorth',
    level: 'high',
    logsource_category: 'process_access',
    logsource_product: 'windows',
    logsource_service: 'sysmon',
    detection_yaml: 'title: LSASS\nlogsource:\n  product: windows\ndetection:\n  condition: selection',
    mitre_attack_ids: '["T1003.001"]',
    false_positives: '["Backup agents"]',
    tags: '["attack.credential_access"]',
    is_enabled: true,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
  };

  /** Minimal stand-in for the chip input's commit event. */
  function chipEvent(value: string): MatChipInputEvent {
    return {
      value,
      chipInput: { clear: () => undefined } as any,
      input: null as any,
    } as MatChipInputEvent;
  }

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService',
      ['get', 'post', 'patch', 'delete', 'aiDetectionDraft']);
    mockHttp = jasmine.createSpyObj('HttpClient', ['get']);

    mockApi.get.and.returnValue(of([]));
    mockApi.post.and.returnValue(of({}));
    mockApi.patch.and.returnValue(of({}));
    mockApi.delete.and.returnValue(of(undefined));
    mockApi.aiDetectionDraft.and.returnValue(of({ output: 'title: drafted', model_used: 'local' }));
    mockHttp.get.and.returnValue(of([{ id: 'T1059.001', name: 'PowerShell' }]));

    await TestBed.configureTestingModule({
      imports: [DetectionEditorComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: mockApi },
        { provide: HttpClient, useValue: mockHttp },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(DetectionEditorComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('loads the first page with limit and offset', () => {
    fixture.detectChanges();
    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?limit=20&offset=0');
  });

  it('loads MITRE suggestions from the asset and tolerates failure', () => {
    fixture.detectChanges();
    expect(mockHttp.get).toHaveBeenCalledWith('/assets/mitre-common.json');
    expect(component.suggestions().length).toBe(1);

    mockHttp.get.and.returnValue(throwError(() => new Error('404')));
    const second = TestBed.createComponent(DetectionEditorComponent);
    second.detectChanges();
    expect(second.componentInstance.suggestions()).toEqual([]);
  });

  // ── (1) filter changes reach the service with the right params ──
  it('sends the status filter to the server', () => {
    fixture.detectChanges();
    mockApi.get.calls.reset();

    component.onStatusChange('testing');

    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?status=testing&limit=20&offset=0');
  });

  it('sends the level filter to the server', () => {
    fixture.detectChanges();
    mockApi.get.calls.reset();

    component.onLevelChange('critical');

    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?level=critical&limit=20&offset=0');
  });

  it('debounces the search input before refetching', fakeAsync(() => {
    fixture.detectChanges();
    mockApi.get.calls.reset();

    component.onSearchInput('lsa');
    component.onSearchInput('lsass');
    tick(100);
    expect(mockApi.get).not.toHaveBeenCalled();

    tick(300);
    expect(mockApi.get).toHaveBeenCalledTimes(1);
    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?search=lsass&limit=20&offset=0');
  }));

  it('combines filters and resets the offset when one changes', fakeAsync(() => {
    fixture.detectChanges();
    component.filterStatus = 'stable';
    component.filterLevel = 'high';
    component.offset.set(40);
    mockApi.get.calls.reset();

    component.onSearchInput('cred');
    tick(300);

    expect(mockApi.get).toHaveBeenCalledWith(
      '/detection-rules?status=stable&level=high&search=cred&limit=20&offset=0',
    );
    expect(component.offset()).toBe(0);
  }));

  it('pages forward and back through the server-side offset', () => {
    mockApi.get.and.returnValue(of(new Array(20).fill(rule)));
    fixture.detectChanges();
    mockApi.get.calls.reset();

    component.nextPage();
    expect(component.offset()).toBe(20);
    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?limit=20&offset=20');

    component.prevPage();
    expect(component.offset()).toBe(0);
    expect(mockApi.get).toHaveBeenCalledWith('/detection-rules?limit=20&offset=0');
  });

  it('does not page past the last full page', () => {
    mockApi.get.and.returnValue(of([rule]));
    fixture.detectChanges();
    mockApi.get.calls.reset();

    component.nextPage();

    expect(component.offset()).toBe(0);
    expect(mockApi.get).not.toHaveBeenCalled();
  });

  // ── (2) a 422 with the nested detail shape renders its messages ──
  it('unpacks a 422 detail object and renders every message', () => {
    fixture.detectChanges();
    component.newRule();
    mockApi.post.and.returnValue(throwError(() => ({
      status: 422,
      error: {
        detail: {
          message: 'Invalid Sigma rule',
          errors: ["Missing required field: 'detection'", "'detection' must include a 'condition' field"],
        },
      },
    })));

    component.saveRule();
    fixture.detectChanges();

    expect(component.saveErrors()).toEqual([
      'Invalid Sigma rule',
      "Missing required field: 'detection'",
      "'detection' must include a 'condition' field",
    ]);
    expect(component.saving()).toBeFalse();
    expect(component.editing()).toBeTrue();

    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).toContain('Invalid Sigma rule');
    expect(text).toContain("Missing required field: 'detection'");
    expect(text).toContain("'detection' must include a 'condition' field");
  });

  it('falls back to a plain string detail', () => {
    fixture.detectChanges();
    component.newRule();
    mockApi.post.and.returnValue(throwError(() => ({ error: { detail: 'Not permitted' } })));

    component.saveRule();

    expect(component.saveErrors()).toEqual(['Not permitted']);
  });

  // ── (3) an invalid MITRE id is rejected by the chip input ──
  it('rejects an id that is not an ATT&CK technique', () => {
    fixture.detectChanges();
    component.newRule();

    component.addMitre(chipEvent('not-a-technique'));

    expect(component.editMitre()).toEqual([]);
    expect(component.mitreError()).toContain('NOT-A-TECHNIQUE');
  });

  it('accepts both technique and sub-technique ids and clears the error', () => {
    fixture.detectChanges();
    component.newRule();

    component.addMitre(chipEvent('bogus'));
    expect(component.mitreError()).toBeTruthy();

    component.addMitre(chipEvent('t1059'));
    component.addMitre(chipEvent('T1059.001'));

    expect(component.editMitre()).toEqual(['T1059', 'T1059.001']);
    expect(component.mitreError()).toBe('');
  });

  it('does not add duplicate techniques and can remove one', () => {
    fixture.detectChanges();
    component.newRule();

    component.addMitre(chipEvent('T1003.001'));
    component.addMitre(chipEvent('T1003.001'));
    expect(component.editMitre()).toEqual(['T1003.001']);

    component.removeMitre('T1003.001');
    expect(component.editMitre()).toEqual([]);
  });

  // ── Metadata round-trip ──
  it('hydrates the metadata fields from a rule, JSON list columns included', () => {
    fixture.detectChanges();

    component.editRule(rule as any);

    expect(component.editSigmaId).toBe('tn-detect-lsass');
    expect(component.editLogsourceService).toBe('sysmon');
    expect(component.editMitre()).toEqual(['T1003.001']);
    expect(component.editFalsePositives()).toEqual(['Backup agents']);
    expect(component.editTags()).toEqual(['attack.credential_access']);
    expect(component.editEnabled).toBeTrue();
  });

  it('sends all eight metadata fields on update', () => {
    fixture.detectChanges();
    component.editRule(rule as any);
    component.addTag(chipEvent('attack.t1003'));

    component.saveRule();

    expect(mockApi.patch).toHaveBeenCalledWith('/detection-rules/r-1', jasmine.objectContaining({
      sigma_id: 'tn-detect-lsass',
      logsource_category: 'process_access',
      logsource_product: 'windows',
      logsource_service: 'sysmon',
      mitre_attack_ids: ['T1003.001'],
      false_positives: ['Backup agents'],
      tags: ['attack.credential_access', 'attack.t1003'],
      is_enabled: true,
    }));
  });

  // ── Validation UX ──
  it('keeps warnings alongside a valid result', () => {
    fixture.detectChanges();
    component.newRule();
    mockApi.post.and.returnValue(of({
      valid: true, errors: [], warnings: ["Non-standard level 'urgent'"],
    }));

    component.validateYaml();
    fixture.detectChanges();

    expect(mockApi.post).toHaveBeenCalledWith('/detection-rules/validate', { yaml: component.editYaml });
    expect(component.validation()!.warnings.length).toBe(1);
    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).toContain("Non-standard level 'urgent'");
  });

  // ── AI drafting ──
  it('puts the AI output into the editor and prefills the technique', () => {
    fixture.detectChanges();
    component.newRule();

    (component as any).runAiDraft({ technique: 'T1059.001', data_source: 'sysmon', format: 'sigma' });

    expect(mockApi.aiDetectionDraft).toHaveBeenCalledWith({
      technique: 'T1059.001', data_source: 'sysmon', format: 'sigma',
    });
    expect(component.editYaml).toBe('title: drafted');
    expect(component.editMitre()).toEqual(['T1059.001']);
    expect(component.aiDrafting()).toBeFalse();
  });

  it('clears the drafting state when the AI call fails', () => {
    fixture.detectChanges();
    component.newRule();
    mockApi.aiDetectionDraft.and.returnValue(throwError(() => new Error('502')));

    (component as any).runAiDraft({ technique: 'T1059', data_source: 'zeek', format: 'sigma' });

    expect(component.aiDrafting()).toBeFalse();
    expect(component.editMitre()).toEqual([]);
  });
});
