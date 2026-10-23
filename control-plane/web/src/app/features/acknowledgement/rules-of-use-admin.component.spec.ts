import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { signal } from '@angular/core';
import { Router } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { AcknowledgementApiService, AcknowledgementVersion } from '@core/services/acknowledgement-api.service';
import { RulesOfUseAdminComponent } from './rules-of-use-admin.component';

const PUBLISHED = { id: 'v1', version: 1, text: '# One', published_at: '2026-10-01T00:00:00Z' } as AcknowledgementVersion;
const DRAFT = { id: 'd1', version: null, text: '# Draft', published_at: null, created_at: '2026-10-22T00:00:00Z' } as
  AcknowledgementVersion;

describe('RulesOfUseAdminComponent', () => {
  let fixture: ComponentFixture<RulesOfUseAdminComponent>;
  let component: RulesOfUseAdminComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<AcknowledgementApiService>;
  let auth: { isPlatformAdmin: ReturnType<typeof signal<boolean>>; setAcknowledgementRequired: jasmine.Spy };
  let router: jasmine.SpyObj<Router>;
  let confirmed: boolean;

  function setup(platform: boolean) {
    api = jasmine.createSpyObj('AcknowledgementApiService',
      ['versions', 'createDraft', 'updateDraft', 'publish', 'exportSignatures']);
    api.versions.and.returnValue(of([DRAFT, PUBLISHED]));
    api.createDraft.and.returnValue(of({ ...DRAFT, id: 'd2' }));
    api.updateDraft.and.callFake((id: string, text: string) => of({ ...DRAFT, id, text }));
    api.publish.and.returnValue(of({ ...DRAFT, version: 2, published_at: '2026-10-23T00:00:00Z' }));
    api.exportSignatures.and.returnValue(of(new Blob(['signed_at\n'], { type: 'text/csv' })));
    auth = { isPlatformAdmin: signal(platform), setAcknowledgementRequired: jasmine.createSpy('set') };
    router = jasmine.createSpyObj('Router', ['navigate']);
    router.navigate.and.resolveTo(true);
    confirmed = true;
    const dialog = { open: () => ({ afterClosed: () => of(confirmed) }) };
    TestBed.configureTestingModule({
      imports: [RulesOfUseAdminComponent, NoopAnimationsModule],
      providers: [
        { provide: AcknowledgementApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: Router, useValue: router },
        { provide: MatDialog, useValue: dialog },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
      ],
    });
    fixture = TestBed.createComponent(RulesOfUseAdminComponent);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  const button = (label: string) =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes(label)) as HTMLButtonElement | undefined;

  it('a tenant admin only exports', () => {
    setup(false);
    expect(api.versions).not.toHaveBeenCalled();
    expect(button('Export signatures')).toBeTruthy();
    expect(button('New draft')).toBeUndefined();
    expect(el.textContent).toContain('Your organisation');
  });

  it('the platform admin sees drafts and versions, the draft open for editing', () => {
    setup(true);
    expect(el.textContent).toContain('Version 1');
    expect(el.textContent).toContain('(current)');
    expect(component.selected()?.id).toBe('d1');
    expect(el.querySelector('textarea')).toBeTruthy();
  });

  it('a published version is read-only', () => {
    setup(true);
    component.select(PUBLISHED);
    fixture.detectChanges();
    expect(el.querySelector('textarea')).toBeNull();
    expect(el.textContent).toContain('read-only');
    expect(button('Publish')).toBeUndefined();
  });

  it('publishing saves unsaved edits first, then sends the publisher to sign', () => {
    setup(true);
    component.draftText = '# Draft, edited';
    component.dirty.set(true);
    fixture.detectChanges();
    button('Publish')!.click();
    expect(api.updateDraft).toHaveBeenCalledWith('d1', '# Draft, edited');
    expect(api.publish).toHaveBeenCalledWith('d1');
    expect(auth.setAcknowledgementRequired).toHaveBeenCalledWith(true);
    expect(router.navigate).toHaveBeenCalledWith(['/rules-of-use'], { queryParams: { returnUrl: '/admin/rules-of-use' } });
  });

  it('does nothing when publishing is cancelled', () => {
    setup(true);
    confirmed = false;
    button('Publish')!.click();
    expect(api.publish).not.toHaveBeenCalled();
  });

  it('a new draft starts from the current version', () => {
    setup(true);
    button('New draft')!.click();
    expect(api.createDraft).toHaveBeenCalledWith('# One');
  });
});
