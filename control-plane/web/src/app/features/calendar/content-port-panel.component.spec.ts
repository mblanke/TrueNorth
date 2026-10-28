import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import {
  ContentPortApiService, PortImport, PortPreview, ProvenStatus,
} from '@core/services/content-port-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ContentPortPanelComponent, exportProblems } from './content-port-panel.component';

const ITEMS = [
  { item_type: 'course', item_id: 'c1', title: 'C201', proven: true, label: 'Proven', reasons: [], conditions: [] },
  { item_type: 'range_template', item_id: 't1', title: 'SOC lab', proven: false,
    label: 'Not proven: No range built from it has reached Certified yet', reasons: [], conditions: [] },
] as unknown as ProvenStatus[];

const PREVIEW = {
  bundle_id: 'b1', exported_at: '2026-10-27T10:00:00Z', key_id: 'ab12cd34ef56ab12', signed_sha256: 'x',
  target_tenant_id: 'tn', shared: false, already_imported_at: null, can_import: true,
  items: [
    { key: 'range_template:t1', item_type: 'range_template', name: 'SOC lab', action: 'conflict',
      conflict: 'local_changed', reasons: ['It was changed here since it was imported'], notes: [],
      diff: '--- here\n+++ bundle\n-os: srv2019\n+os: srv2022', proven_at_origin: true },
    { key: 'course:c1', item_type: 'course', name: 'C201', action: 'create', reasons: [],
      notes: ['A new course lands unpublished'], diff: '', proven_at_origin: true },
  ],
  missing_golden_builds: [{ catalogue_id: 'srv2022', build_id: 'b-1', checksum: '', needed_by: ['range_template:t1'] }],
} as unknown as PortPreview;

describe('ContentPortPanelComponent', () => {
  let fixture: ComponentFixture<ContentPortPanelComponent>;
  let component: ContentPortPanelComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ContentPortApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  const button = (text: string) =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes(text)) as HTMLButtonElement;

  beforeEach(async () => {
    api = jasmine.createSpyObj('ContentPortApiService',
      ['items', 'signingKey', 'exportBundle', 'preview', 'importBundle']);
    api.items.and.returnValue(of(ITEMS));
    api.signingKey.and.returnValue(of({ alg: 'Ed25519', key_id: 'ab12cd34ef56ab12', public_key: 'PUBKEY=' }));
    api.preview.and.returnValue(of(PREVIEW));
    api.importBundle.and.returnValue(of({ import_id: 'i', bundle_id: 'b1', created: 1, updated: 1, unchanged: 0,
      skipped: 0, items: [], missing_golden_builds: [] } as unknown as PortImport));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);
    await TestBed.configureTestingModule({
      imports: [ContentPortPanelComponent, NoopAnimationsModule],
      providers: [
        { provide: ContentPortApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ContentPortPanelComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  });

  it('lets only proven content be picked for export, and says why the rest is not', () => {
    button('Choose content to export').click();
    fixture.detectChanges();
    const boxes = Array.from(el.querySelectorAll('mat-checkbox input')) as HTMLInputElement[];
    expect(boxes[0].disabled).toBeFalse();
    expect(boxes[1].disabled).toBeTrue();
    expect(el.querySelector('.why')?.textContent).toContain('No range built from it');
    expect(button('Export proven content').disabled).toBeTrue();
    component.toggle(ITEMS[0]);
    fixture.detectChanges();
    expect(button('Export proven content').disabled).toBeFalse();
  });

  it('lists what is not proven when the export is refused', () => {
    button('Choose content to export').click();
    component.toggle(ITEMS[0]);
    const body = JSON.stringify({ detail: { message: 'Only proven content is exported', items: [
      { item_type: 'range_template', item_id: 't1', title: 'SOC lab', dependency: true,
        reasons: ['No range built from it has reached Certified yet'] }] } });
    api.exportBundle.and.returnValue(throwError(() => new HttpErrorResponse({ status: 409, error: body })));
    component.exportPicked();
    fixture.detectChanges();
    expect(api.exportBundle).toHaveBeenCalledWith([{ item_type: 'course', item_id: 'c1' }]);
    const refusal = el.querySelector('.refusal')?.textContent ?? '';
    expect(refusal).toContain('Only proven content is exported');
    expect(refusal).toContain('SOC lab');
    expect(refusal).toContain('(needed)');
  });

  it('previews a bundle and imports only the conflicts it is told to replace', () => {
    const file = new File(['{}'], 'bundle.json', { type: 'application/json' });
    component.shared = true;
    component.pickedFile({ files: [file], value: 'x' } as unknown as HTMLInputElement);
    fixture.detectChanges();
    expect(api.preview).toHaveBeenCalledWith(file, { shared: true });
    const items = Array.from(el.querySelectorAll('.pitem'));
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain('Conflict');
    expect(items[0].textContent).toContain('changed here');
    expect(el.querySelector('.missing')?.textContent).toContain('srv2022 build b-1');
    button('Import').click();
    expect(api.importBundle).toHaveBeenCalledWith(file, { shared: true }, []);
    component.pickedFile({ files: [file], value: 'x' } as unknown as HTMLInputElement);
    component.toggleOverwrite('range_template:t1');
    component.importNow();
    expect(api.importBundle).toHaveBeenCalledWith(file, { shared: true }, ['range_template:t1']);
    expect(notify.success).toHaveBeenCalledWith(jasmine.stringMatching('None is proven here yet'));
  });

  it("shows this site's key for the importing site", () => {
    button('Show').click();
    fixture.detectChanges();
    expect(el.querySelector('.pub')?.textContent).toContain('PUBKEY=');
  });

  it('reads a refusal that is not a list as its message', () => {
    const e = new HttpErrorResponse({ status: 409, error: JSON.stringify({ detail: 'TN_SECRETS_KEY is not set' }) });
    expect(exportProblems(e)).toEqual({ message: 'TN_SECRETS_KEY is not set', items: [] });
  });
});
