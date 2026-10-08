import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';

import { MatDialog } from '@angular/material/dialog';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { IntegrationsComponent } from './integrations.component';

describe('IntegrationsComponent', () => {
  let fixture: ComponentFixture<IntegrationsComponent>;
  let component: IntegrationsComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ApiService>;
  let alertSpy: jasmine.Spy;
  let confirmSpy: jasmine.Spy;
  let notify: jasmine.SpyObj<NotificationService>;
  let dialogOpen: jasmine.Spy;
  let dialogResult: boolean;

  afterEach(() => {
    expect(alertSpy).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  const PLATFORMS = [
    {
      id: 'p1', name: 'Moodle LMS', slug: 'moodle', platform_type: 'moodle',
      base_url: 'https://moodle.example.test', auth_type: 'lti13', is_active: true,
      last_sync_at: '2026-10-01T12:00:00Z',
    },
    {
      id: 'p2', name: 'OffSec', slug: 'offsec', platform_type: 'offsec',
      base_url: 'https://offsec.example.test', auth_type: 'api_key', is_active: false,
      last_sync_at: null,
    },
  ];

  async function render(list: unknown = of(PLATFORMS)): Promise<void> {
    api = jasmine.createSpyObj('ApiService', ['get', 'post', 'patch', 'delete']);
    api.get.and.returnValue(list as any);
    api.post.and.returnValue(of({}));
    api.patch.and.returnValue(of({}));
    api.delete.and.returnValue(of(undefined));
    // Native dialogs must never be used; spy so a regression fails loudly instead of blocking.
    alertSpy = spyOn(window, 'alert');
    confirmSpy = spyOn(window, 'confirm').and.returnValue(true);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    dialogResult = true;
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of(dialogResult) }));

    await TestBed.configureTestingModule({
      imports: [IntegrationsComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(IntegrationsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const cardTitles = () =>
    Array.from(el.querySelectorAll('.platform-card mat-card-title')).map(t => t.textContent?.trim());

  it('creates and loads platforms from /integrations/platforms', async () => {
    await render();
    expect(component).toBeTruthy();
    expect(api.get).toHaveBeenCalledWith('/integrations/platforms');
    expect(component.loading()).toBeFalse();
    expect(cardTitles()).toEqual(['Moodle LMS', 'OffSec']);
    expect(el.textContent).toContain('Active');
    expect(el.textContent).toContain('Inactive');
    expect(el.querySelector('tn-empty-state')).toBeNull();
  });

  it('shows the empty state when no platforms are connected', async () => {
    await render(of([]));
    expect(el.querySelector('tn-empty-state')?.textContent).toContain('No Platforms Connected');
  });

  it('falls back to the empty state, not a crash, when the list request fails', async () => {
    await render(throwError(() => ({ status: 500 })));
    expect(component.loading()).toBeFalse();
    expect(component.platforms()).toEqual([]);
    expect(el.querySelector('tn-empty-state')).not.toBeNull();
    expect(el.querySelector('[aria-busy="true"]')).toBeNull();
  });

  it('creates a platform with POST and reloads the list', async () => {
    await render();
    component.showAdd = true;
    component.newPlatform = {
      name: 'Immersive', slug: 'il', base_url: 'https://il.test', auth_type: 'oauth2', platform_type: 'immersive_labs',
    };
    component.addPlatform();
    expect(api.post).toHaveBeenCalledWith('/integrations/platforms', jasmine.objectContaining({ slug: 'il' }));
    expect(component.showAdd).toBeFalse();
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('shows the server detail in a snackbar when creating a platform fails', async () => {
    await render();
    api.post.and.returnValue(throwError(() => ({ error: { detail: 'Slug already exists' } })));
    component.addPlatform();
    expect(notify.error).toHaveBeenCalledWith('Slug already exists');
  });

  it('edits a platform with PATCH to its id and resets the form', async () => {
    await render();
    const editBtn = Array.from(el.querySelectorAll<HTMLButtonElement>('.platform-card button'))
      .find(b => b.textContent?.includes('Edit'))!;
    editBtn.click();
    fixture.detectChanges();
    expect(component.editingPlatformId).toBe('p1');
    expect(el.textContent).toContain('Edit Platform');
    expect(component.newPlatform.name).toBe('Moodle LMS');

    component.newPlatform.name = 'Moodle Prod';
    component.updatePlatform();
    expect(api.patch).toHaveBeenCalledWith('/integrations/platforms/p1', jasmine.objectContaining({ name: 'Moodle Prod' }));
    expect(component.editingPlatformId).toBeNull();
    expect(component.platformSaving).toBeFalse();
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('keeps the edit open and reports when PATCH fails', async () => {
    await render();
    component.startEditPlatform(PLATFORMS[1]);
    api.patch.and.returnValue(throwError(() => ({ error: {} })));
    component.updatePlatform();
    expect(component.editingPlatformId).toBe('p2');
    expect(component.platformSaving).toBeFalse();
    expect(notify.error).toHaveBeenCalledWith('Failed to update platform');
  });

  it('tests a connection via POST /integrations/platforms/:id/test and reports the outcome', async () => {
    await render();
    api.post.and.returnValue(of({ reachable: true, status_code: 200 }));
    component.testConnection(PLATFORMS[0]);
    expect(api.post).toHaveBeenCalledWith('/integrations/platforms/p1/test', {});
    expect(notify.success).toHaveBeenCalledWith('Connection OK: Status 200');

    api.post.and.returnValue(of({ reachable: false, error: 'timeout' }));
    component.testConnection(PLATFORMS[0]);
    expect(notify.error).toHaveBeenCalledWith('Connection Failed: timeout');
  });

  it('reports a failed connection test request', async () => {
    await render();
    api.post.and.returnValue(throwError(() => ({ error: { detail: 'Platform not found' } })));
    component.testConnection(PLATFORMS[0]);
    expect(notify.error).toHaveBeenCalledWith('Test failed: Platform not found');
  });

  it('deletes a platform after Material confirmation and reloads', async () => {
    await render();
    component.deletePlatform(PLATFORMS[1]);
    expect(dialogOpen).toHaveBeenCalledWith(ConfirmDialogComponent, jasmine.objectContaining({
      data: jasmine.objectContaining({ title: 'Remove platform', confirmText: 'Remove' }),
    }));
    expect(dialogOpen.calls.mostRecent().args[1].data.message).toContain('Remove OffSec?');
    expect(api.delete).toHaveBeenCalledWith('/integrations/platforms/p2');
    expect(notify.success).toHaveBeenCalledWith('OffSec removed');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('does not delete when the confirmation is declined', async () => {
    await render();
    dialogResult = false;
    component.deletePlatform(PLATFORMS[1]);
    expect(dialogOpen).toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
  });

  it('reports when delete fails', async () => {
    await render();
    api.delete.and.returnValue(throwError(() => ({ error: { detail: 'In use' } })));
    component.deletePlatform(PLATFORMS[0]);
    expect(notify.error).toHaveBeenCalledWith('In use');
    expect(api.get).toHaveBeenCalledTimes(1);
  });
});
