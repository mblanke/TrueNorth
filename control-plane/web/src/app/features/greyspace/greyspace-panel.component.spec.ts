import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { GreyspaceApiService, GreyspaceStatus } from '@core/services/greyspace-api.service';
import { NotificationService } from '@core/services/notification.service';
import { GreyspacePanelComponent } from './greyspace-panel.component';

const CORPORA = [
  { tier: 't0', title: 'CI fixture', cap_bytes: 100000000, location: 'generated', builder: 'b', description: 'd',
    available: true, version: '2026.10-t0', sites: 20, bytes: 1, categories: { news: 4, dev: 3 }, threat_domains: 2 },
  { tier: 't2', title: 'Lab corpus', cap_bytes: 50000000000, location: 'NetApp', builder: 'b', description: 'd',
    available: false },
] as any;

function status(extra: Partial<GreyspaceStatus> = {}): GreyspaceStatus {
  return { range_id: 'r1', attached: false, status: 'not_attached', range_state: 'created', problems: [], ...extra } as GreyspaceStatus;
}

describe('GreyspacePanelComponent', () => {
  let fixture: ComponentFixture<GreyspacePanelComponent>;
  let api: jasmine.SpyObj<GreyspaceApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  function create(initial: GreyspaceStatus) {
    api.status.and.returnValue(of(initial));
    fixture = TestBed.createComponent(GreyspacePanelComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.detectChanges();
    TestBed.flushEffects();
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  beforeEach(() => {
    api = jasmine.createSpyObj('GreyspaceApiService', ['corpora', 'status', 'attach', 'detach', 'config']);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    api.corpora.and.returnValue(of(CORPORA));
    api.config.and.returnValue(of({ range_id: 'r1', corpus_tier: 't0', corpus_version: 'v', services: ['webfarm', 'resolver'],
      sites: 7, site_packs: ['dev', 'news'], tlds: ['com'], zones: ['a.com'], threat_domains: [],
      address_plan: { resolver: '198.18.0.53' }, isps: [{ asn: 64501, prefix: '198.18.0.0/16' }], files: [] } as any));
    TestBed.configureTestingModule({
      imports: [GreyspacePanelComponent],
      providers: [
        provideNoopAnimations(),
        provideRouter([]),
        { provide: GreyspaceApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    });
  });

  it('loads the status for its range and shows it is not attached', () => {
    create(status());
    expect(api.status).toHaveBeenCalledWith('r1');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[data-test="gs-status"]')?.textContent).toContain('Not attached');
    expect(el.querySelector('[data-test="gs-save"]')?.textContent).toContain('Attach Greyspace');
    expect(el.querySelector('[data-test="gs-detach"]')).toBeNull();
  });

  it('attaches with every pack ticked as "all packs" and shows the generated config', () => {
    const c = create(status());
    expect(c.packs().map(p => p.name)).toEqual(['news', 'dev']);
    api.attach.and.returnValue(of(status({ attached: true, status: 'configured',
      block: { version: 1, corpus_tier: 't0', site_packs: null, npc_profile: 'off', threat_infra: true, trust_ca: true },
      corpus: CORPORA[0] })));
    c.save();
    fixture.detectChanges();
    const [rangeId, block] = api.attach.calls.mostRecent().args;
    expect(rangeId).toBe('r1');
    expect(block?.site_packs).toBeNull();
    expect(block?.public_prefix).toBeNull();
    expect(notify.success).toHaveBeenCalled();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[data-test="gs-status"]')?.textContent).toContain('Configured');
    expect(el.querySelector('[data-test="gs-config"]')?.textContent).toContain('198.18.0.53');
  });

  it('sends only the ticked packs when some are unticked', () => {
    const c = create(status());
    c.draft.packs['dev'] = false;
    c.draft.public_prefix = ' 198.16.0.0/12 ';
    expect(c.toBlock().site_packs).toEqual(['news']);
    expect(c.toBlock().public_prefix).toBe('198.16.0.0/12');
  });

  it("starts from the template's block when nothing is attached", () => {
    const c = create(status({ template_block: { version: 1, corpus_tier: 't0', site_packs: ['news'], npc_profile: 'off',
      threat_infra: false, trust_ca: true } }));
    expect(c.draft.packs).toEqual({ news: true, dev: false });
    expect(c.draft.threat_infra).toBeFalse();
  });

  it('shows the problems a 422 lists', () => {
    const c = create(status());
    api.attach.and.returnValue(throwError(() => ({ status: 422, error: { detail: { problems: ['site_packs [x] are not in this corpus'] } } })));
    c.save();
    fixture.detectChanges();
    expect(c.error()).toContain('not in this corpus');
    expect(fixture.nativeElement.querySelector('[role="alert"]')?.textContent).toContain('not in this corpus');
  });

  it('detaches and reloads', () => {
    const c = create(status({ attached: true, status: 'deployed', corpus: CORPORA[0],
      block: { version: 1, corpus_tier: 't0', site_packs: null, npc_profile: 'off', threat_infra: true, trust_ca: true } }));
    expect(fixture.nativeElement.querySelector('[data-test="gs-detach"]')).not.toBeNull();
    api.detach.and.returnValue(of(undefined));
    api.status.and.returnValue(of(status()));
    c.detach();
    expect(api.detach).toHaveBeenCalledWith('r1');
    expect(c.status()?.attached).toBeFalse();
  });

  it('says so when the range is not found', () => {
    api.status.and.returnValue(throwError(() => ({ status: 404 })));
    fixture = TestBed.createComponent(GreyspacePanelComponent);
    fixture.componentRef.setInput('rangeId', 'nope');
    fixture.detectChanges();
    TestBed.flushEffects();
    fixture.detectChanges();
    expect(fixture.componentInstance.error()).toBe('Range not found');
  });

  it('switching to a tier the control plane cannot read clears the packs', () => {
    const c = create(status());
    c.draft.corpus_tier = 't2';
    c.onTier();
    expect(c.packs()).toEqual([]);
    expect(c.toBlock().site_packs).toBeNull();
  });
});
