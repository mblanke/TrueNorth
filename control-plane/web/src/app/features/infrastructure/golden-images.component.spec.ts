import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { GoldenImage, GoldenImageBuild, GoldenImageBuilds } from '@core/models';
import { GoldenImagesApiService } from '@core/services/golden-images-api.service';
import { GoldenImagesComponent, shortSum } from './golden-images.component';

const SUM_A = 'a'.repeat(64);
const SUM_B = 'b'.repeat(64);

const build = (id: string, checksum: string, promoted = false): GoldenImageBuild => ({
  id: `uuid-${id}`, build_id: id, template_name: 'tmpl-ubuntu-2404', checksum, build_date: '2026-10-19',
  iso_checksum: 'sha256:' + 'c'.repeat(64), copies: { esx02: 'tmpl-ubuntu-2404@esx02-20261019' },
  registered_by: 'ops@example.org', registered_at: '2026-10-19T12:00:00Z', promoted,
  promoted_at: promoted ? '2026-10-20T09:00:00Z' : null, promoted_by: promoted ? 'admin@example.org' : '',
  promotable: true, not_promotable_reason: '', missing_hosts: [],
});

const image = (promoted: GoldenImageBuild | null): GoldenImage => ({
  id: 'img-1', catalogue_id: 'ubuntu-2404', os_family: 'linux', version: '24.04', role: '', hypervisor: 'vsphere',
  template_name: 'tmpl-ubuntu-2404', datastore: '', os_aliases: [], sensor_baked: false, enabled: true,
  build_status: 'built', golden_gb: 0, notes: '', promoted_build: promoted,
});

const detail = (builds: GoldenImageBuild[], audit: GoldenImageBuilds['audit'] = [],
                canPromote = true): GoldenImageBuilds => ({
  image_id: 'img-1', catalogue_id: 'ubuntu-2404', can_promote: canPromote,
  promoted_build_id: builds.find(b => b.promoted)?.build_id ?? null, builds, audit,
});

describe('GoldenImagesComponent (builds and promotion, ADR 0013)', () => {
  let api: jasmine.SpyObj<GoldenImagesApiService>;
  let fixture: ComponentFixture<GoldenImagesComponent>;

  const el = () => fixture.nativeElement as HTMLElement;
  const q = (id: string) => el().querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

  function create(): void {
    TestBed.configureTestingModule({
      imports: [GoldenImagesComponent, NoopAnimationsModule],
      providers: [
        { provide: GoldenImagesApiService, useValue: api },
      ],
    });
    fixture = TestBed.createComponent(GoldenImagesComponent);
    fixture.detectChanges();
  }

  function openBuilds(): void {
    q('gi-open-ubuntu-2404')!.click();
    fixture.detectChanges();
  }

  beforeEach(() => {
    api = jasmine.createSpyObj('GoldenImagesApiService', ['list', 'builds', 'promote']);
    api.list.and.returnValue(of([image(null)]));
    api.builds.and.returnValue(of(detail([build('b2', SUM_B), build('b1', SUM_A)])));
  });

  it('lists images with their promoted build, none until one is promoted', () => {
    create();
    const cells = Array.from(el().querySelectorAll('tr.mat-mdc-row td')).map(td => td.textContent?.trim());
    expect(cells.slice(0, 4)).toEqual(['ubuntu-2404', 'built', 'none', '—']);
  });

  it('shows each build with its checksum, dates and host copies', () => {
    create();
    openBuilds();
    expect(api.builds).toHaveBeenCalledOnceWith('img-1');
    expect(q('gi-build-b1')!.textContent).toContain('aaaaaaaaaaaa…');
    expect(q('gi-build-b1')!.textContent).toContain('2026-10-19');
    expect(q('gi-copies')!.textContent).toContain('esx02: tmpl-ubuntu-2404@esx02-20261019');
  });

  it('lets an admin promote a build, with the reason, and shows it promoted with its audit row', () => {
    const after = detail([build('b2', SUM_B), build('b1', SUM_A, true)], [
      { timestamp: '2026-10-20T09:00:00Z', action: 'golden_image.promote', actor: 'admin@example.org',
        detail: { build_id: 'b1' } }]);
    api.promote.and.returnValue(of(after));
    spyOn(window, 'confirm').and.returnValue(true);
    create();
    openBuilds();
    const reason = q('gi-reason') as HTMLInputElement;
    reason.value = 'passed acceptance';
    reason.dispatchEvent(new Event('input'));
    q('gi-promote-b1')!.click();
    fixture.detectChanges();
    expect(api.promote).toHaveBeenCalledOnceWith('img-1', 'b1', 'passed acceptance');
    expect(q('gi-build-b1')!.querySelector('[data-testid="gi-promoted-badge"]')).not.toBeNull();
    expect(q('gi-promote-b1')).toBeNull();
    expect(q('gi-audit')!.textContent).toContain('promoted b1');
    expect(q('gi-audit')!.textContent).toContain('admin@example.org');
    expect(api.list).toHaveBeenCalledTimes(2);  // the table shows the new promoted build
  });

  it('disables Promote for a build with no host copies, with the reason', () => {
    const incomplete = { ...build('b3', SUM_B), copies: {}, promotable: false,
      not_promotable_reason: 'no host copies recorded yet' };
    api.builds.and.returnValue(of(detail([incomplete])));
    create();
    openBuilds();
    expect((q('gi-promote-b3') as HTMLButtonElement).disabled).toBeTrue();
    expect(q('gi-unpromotable-b3')!.textContent).toContain('no host copies recorded yet');
    expect(q('gi-reason')).toBeNull();
  });

  it('tells the admin what promoting changes, hosts without a copy included', () => {
    const b = { ...build('b2', SUM_B), missing_hosts: ['esx04'] };
    api.builds.and.returnValue(of(detail([b, build('b1', SUM_A, true)])));
    const confirm = spyOn(window, 'confirm').and.returnValue(false);
    create();
    openBuilds();
    expect(q('gi-missing-b2')!.textContent).toContain('esx04');
    q('gi-promote-b2')!.click();
    const msg = confirm.calls.mostRecent().args[0] as string;
    expect(msg).toContain('It replaces build b1');
    expect(msg).toContain('esx02');
    expect(msg).toContain('will not be used for this image: esx04');
    expect(api.promote).not.toHaveBeenCalled();
  });

  it('does nothing when the admin cancels', () => {
    spyOn(window, 'confirm').and.returnValue(false);
    create();
    openBuilds();
    q('gi-promote-b1')!.click();
    expect(api.promote).not.toHaveBeenCalled();
  });

  it('offers no Promote button to anyone but the platform administrator', () => {
    api.builds.and.returnValue(of(detail([build('b2', SUM_B), build('b1', SUM_A)], [], false)));
    create();
    openBuilds();
    expect(q('gi-build-b1')).not.toBeNull();
    expect(q('gi-promote-b1')).toBeNull();
    expect(q('gi-reason')).toBeNull();
  });

  it('reports a refused promotion', () => {
    api.promote.and.returnValue(throwError(() => ({ error: { detail: 'Permission denied' } })));
    spyOn(window, 'confirm').and.returnValue(true);
    create();
    openBuilds();
    q('gi-promote-b1')!.click();
    fixture.detectChanges();
    expect(q('gi-error')!.textContent).toContain('Promotion failed: Permission denied');
  });

  it('says when no build is registered yet', () => {
    api.builds.and.returnValue(of(detail([])));
    create();
    openBuilds();
    expect(q('gi-no-builds')).not.toBeNull();
  });

  it('shortens checksums', () => {
    expect(shortSum('sha256:' + SUM_A)).toBe('aaaaaaaaaaaa…');
    expect(shortSum('')).toBe('—');
  });
});
