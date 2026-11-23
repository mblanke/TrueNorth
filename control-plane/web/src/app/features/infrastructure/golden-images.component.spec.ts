// Author: Marc-Andre Blanke
// Created: 2026-10-22T02:24:15Z
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { GoldenImage, GoldenImageBuild, GoldenImageBuilds } from '@core/models';
import { GoldenImagesApiService } from '@core/services/golden-images-api.service';
import { GoldenImagesComponent, promoteErrorText, shortSum, viewImages } from './golden-images.component';
import { PromoteBuildDialogData } from './promote-build-dialog.component';

const SUM_A = 'a'.repeat(64);
const SUM_B = 'b'.repeat(64);

const build = (id: string, checksum: string, promoted = false): GoldenImageBuild => ({
  id: `uuid-${id}`, build_id: id, template_name: 'tmpl-ubuntu-2404', checksum, build_date: '2026-10-19',
  iso_checksum: 'sha256:' + 'c'.repeat(64), copies: { esx02: 'tmpl-ubuntu-2404@esx02-20261019' },
  registered_by: 'ops@example.org', registered_at: '2026-10-19T12:00:00Z', promoted,
  promoted_at: promoted ? '2026-10-20T09:00:00Z' : null, promoted_by: promoted ? 'admin@example.org' : '',
  promotable: true, not_promotable_reason: '', missing_hosts: [],
});

const image = (promoted: GoldenImageBuild | null, over: Partial<GoldenImage> = {}): GoldenImage => ({
  id: 'img-1', catalogue_id: 'ubuntu-2404', os_family: 'linux', version: '24.04', role: '', hypervisor: 'vsphere',
  template_name: 'tmpl-ubuntu-2404', datastore: '', os_aliases: [], sensor_baked: false, enabled: true,
  build_status: 'built', golden_gb: 0, notes: '', promoted_build: promoted, ...over,
});

// One image per state, deliberately out of order.
const LIBRARY: GoldenImage[] = [
  image(build('p1', SUM_A, true), { id: 'i-a', catalogue_id: 'alpha-promoted' }),
  image(null, { id: 'i-p', catalogue_id: 'bravo-planned', build_status: 'planned' }),
  image(null, { id: 'i-f', catalogue_id: 'charlie-failed', build_status: 'failed' }),
  image(null, { id: 'i-n', catalogue_id: 'delta-needs', build_status: 'built' }),
  image(null, { id: 'i-d', catalogue_id: 'echo-disabled', enabled: false }),
  image(null, { id: 'i-b', catalogue_id: 'foxtrot-building', build_status: 'building' }),
];
const ids = (xs: GoldenImage[]) => xs.map(i => i.catalogue_id);

const detail = (builds: GoldenImageBuild[], audit: GoldenImageBuilds['audit'] = [],
                canPromote = true): GoldenImageBuilds => ({
  image_id: 'img-1', catalogue_id: 'ubuntu-2404', can_promote: canPromote,
  promoted_build_id: builds.find(b => b.promoted)?.build_id ?? null, builds, audit,
});

describe('GoldenImagesComponent (builds and promotion, ADR 0013)', () => {
  let api: jasmine.SpyObj<GoldenImagesApiService>;
  let fixture: ComponentFixture<GoldenImagesComponent>;
  let dialog: { open: jasmine.Spy };
  /** What the confirm dialog closes with: the reason, or undefined when cancelled. */
  let answer: string | undefined;

  const el = () => fixture.nativeElement as HTMLElement;
  const q = (id: string) => el().querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
  const dialogData = () => dialog.open.calls.mostRecent().args[1].data as PromoteBuildDialogData;

  function create(): void {
    TestBed.configureTestingModule({
      imports: [GoldenImagesComponent, NoopAnimationsModule],
      providers: [
        { provide: GoldenImagesApiService, useValue: api },
      ],
    });
    TestBed.overrideProvider(MatDialog, { useValue: dialog });
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
    answer = undefined;
    dialog = { open: jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of(answer) })) };
  });

  it('lists images with their promoted build, none until one is promoted', () => {
    create();
    const cells = Array.from(el().querySelectorAll('tr.mat-mdc-row td')).map(td => td.textContent?.trim());
    expect(cells.slice(0, 4)).toEqual(['ubuntu-2404', 'built', 'none Needs promotion', '—']);
  });

  it('opens the builds under the image row and closes them on a second click', () => {
    create();
    openBuilds();
    const detailRow = el().querySelector('tr.detail-row.open') as HTMLElement;
    expect(detailRow.querySelector('[data-testid="gi-builds"]')).not.toBeNull();
    expect(q('gi-open-ubuntu-2404')!.getAttribute('aria-expanded')).toBe('true');
    openBuilds();
    expect(q('gi-builds')).toBeNull();
    expect(api.builds).toHaveBeenCalledTimes(1);
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
    answer = 'passed acceptance';
    create();
    openBuilds();
    q('gi-promote-b1')!.click();
    fixture.detectChanges();
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(dialogData().buildId).toBe('b1');
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
  });

  it('tells the admin what promoting changes, hosts without a copy included', () => {
    const b = { ...build('b2', SUM_B), missing_hosts: ['esx04'] };
    api.builds.and.returnValue(of(detail([b, build('b1', SUM_A, true)])));
    create();
    openBuilds();
    expect(q('gi-missing-b2')!.textContent).toContain('esx04');
    q('gi-promote-b2')!.click();
    const msg = dialogData().lines.join('\n');
    expect(msg).toContain('It replaces build b1');
    expect(msg).toContain('esx02');
    expect(msg).toContain('will not be used for this image: esx04');
    expect(api.promote).not.toHaveBeenCalled();
  });

  it('does nothing when the admin cancels', () => {
    answer = undefined;
    create();
    openBuilds();
    q('gi-promote-b1')!.click();
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(api.promote).not.toHaveBeenCalled();
  });

  it('offers no Promote button to anyone but the platform administrator', () => {
    api.builds.and.returnValue(of(detail([build('b2', SUM_B), build('b1', SUM_A)], [], false)));
    create();
    openBuilds();
    expect(q('gi-build-b1')).not.toBeNull();
    expect(el().querySelector('[data-testid^="gi-promote-"]')).toBeNull();
  });

  it('reports a refused promotion in plain words, under the image', () => {
    api.promote.and.returnValue(throwError(() => ({ status: 403, error: { detail: 'Permission denied' } })));
    answer = 'because';
    create();
    openBuilds();
    q('gi-promote-b1')!.click();
    fixture.detectChanges();
    const shown = q('gi-builds')!.querySelector('[data-testid="gi-promote-error"]')!;
    expect(shown.textContent).toContain('Only the platform administrator can promote a build.');
    expect(q('gi-promote-b1')).not.toBeNull();  // still there to try again
  });

  it('words each refusal plainly', () => {
    expect(promoteErrorText({ status: 409 })).toContain('at the same time');
    expect(promoteErrorText({ status: 422, error: { detail: "build 'b1' cannot be promoted: no host copies" } }))
      .toBe("This build cannot be promoted: build 'b1' cannot be promoted: no host copies");
    expect(promoteErrorText({ status: 422, error: { detail: [{ msg: 'String should have at most 500 characters' }] } }))
      .toContain('at most 500 characters');
    expect(promoteErrorText({ status: 0 })).toContain('could not be reached');
  });

  describe('sorting and filtering', () => {
    it('puts images waiting for a promotion first, then failed, building, planned, promoted, disabled', () => {
      expect(ids(viewImages(LIBRARY, 'all', 'all', 'action'))).toEqual([
        'delta-needs', 'charlie-failed', 'foxtrot-building', 'bravo-planned', 'alpha-promoted', 'echo-disabled']);
    });

    it('sorts by name or status on request', () => {
      expect(ids(viewImages(LIBRARY, 'all', 'all', 'name'))[0]).toBe('alpha-promoted');
      expect(ids(viewImages(LIBRARY, 'all', 'all', 'status'))).toEqual([
        'alpha-promoted', 'delta-needs', 'foxtrot-building', 'bravo-planned', 'charlie-failed', 'echo-disabled']);
    });

    it('filters by status and by promoted', () => {
      expect(ids(viewImages(LIBRARY, 'built', 'all', 'name'))).toEqual(['alpha-promoted', 'delta-needs']);
      expect(ids(viewImages(LIBRARY, 'built', 'not_promoted', 'name'))).toEqual(['delta-needs']);
      expect(ids(viewImages(LIBRARY, 'all', 'promoted', 'name'))).toEqual(['alpha-promoted']);
      expect(ids(viewImages(LIBRARY, 'failed', 'promoted', 'name'))).toEqual([]);
      expect(ids(viewImages(LIBRARY, 'disabled', 'all', 'name'))).toEqual(['echo-disabled']);
    });

    it('shows the list needing action first by default, and applies a filter', () => {
      api.list.and.returnValue(of(LIBRARY));
      create();
      const first = () => el().querySelector('tr.mat-mdc-row td')!.textContent!.trim();
      expect(first()).toBe('delta-needs');
      expect(q('gi-needs-delta-needs')).not.toBeNull();
      expect(q('gi-needs-charlie-failed')).toBeNull();
      expect(q('gi-count')!.textContent).toContain('6 of 6');
      expect(q('gi-count')!.textContent).toContain('1 waiting for promotion');

      const cmp = fixture.componentInstance;
      cmp.statusFilter = 'failed';
      cmp.applyView();
      fixture.detectChanges();
      expect(first()).toBe('charlie-failed');
      expect(q('gi-count')!.textContent).toContain('1 of 6');

      cmp.promotedFilter = 'promoted';
      cmp.applyView();
      fixture.detectChanges();
      expect(q('gi-no-match')).not.toBeNull();
    });
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
