import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { MaintenanceWindow, MaintenanceWindowsApiService } from '@core/services/maintenance-windows-api.service';
import { MaintenanceWindowsComponent, isoToLocal, localToIso, toBody } from './maintenance-windows.component';

const WINDOW: MaintenanceWindow = {
  id: 'w1', tenant_id: 't', platform_wide: false, reason: 'vCenter upgrade', scope: 'ranges', range_ids: ['r1'],
  tags: [], active: true, can_edit: true, starts_at: '2026-11-02T10:00:00Z', ends_at: '2026-11-02T12:00:00Z',
} as MaintenanceWindow;
const PLATFORM: MaintenanceWindow = {
  ...WINDOW, id: 'w2', tenant_id: null, platform_wide: true, scope: 'all', range_ids: [], can_edit: false,
  reason: 'Datacentre power work', active: false,
} as MaintenanceWindow;

describe('maintenance window form helpers', () => {
  const form = { reason: 'Upgrade', start: '2026-11-02T22:00', end: '2026-11-03T01:00', scope: 'all' as const,
    rangeIds: [], tags: '', platformWide: false };

  it('sends the times as instants with a zone', () => {
    const body = toBody(form);
    expect(typeof body).toBe('object');
    const b = body as Exclude<typeof body, string>;
    expect(b.starts_at).toBe(new Date('2026-11-02T22:00').toISOString());
    expect(b.starts_at.endsWith('Z')).toBeTrue();
    expect(b.range_ids).toEqual([]);
  });

  it('round-trips a local time', () => {
    expect(isoToLocal(localToIso('2026-03-08T01:30'))).toBe('2026-03-08T01:30');
  });

  it('refuses what the API would refuse, in words', () => {
    expect(toBody({ ...form, reason: 'x' })).toContain('reason');
    expect(toBody({ ...form, end: form.start })).toContain('after the start');
    expect(toBody({ ...form, scope: 'ranges' })).toContain('range');
    expect(toBody({ ...form, scope: 'tags', tags: ' , ' })).toContain('tag');
  });

  it('only sends the list its scope uses', () => {
    const b = toBody({ ...form, scope: 'tags', tags: 'soc, blue', rangeIds: ['r1'] as never[] });
    expect((b as { tags: string[]; range_ids: string[] }).tags).toEqual(['soc', 'blue']);
    expect((b as { range_ids: string[] }).range_ids).toEqual([]);
  });
});

describe('MaintenanceWindowsComponent', () => {
  let fixture: ComponentFixture<MaintenanceWindowsComponent>;
  let component: MaintenanceWindowsComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<MaintenanceWindowsApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  beforeEach(async () => {
    api = jasmine.createSpyObj('MaintenanceWindowsApiService', ['list', 'create', 'update', 'remove']);
    api.list.and.returnValue(of({ items: [WINDOW, PLATFORM], total: 2 }));
    api.create.and.returnValue(of(WINDOW));
    api.remove.and.returnValue(of(undefined));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    const core = jasmine.createSpyObj('ApiService', ['listRanges']);
    core.listRanges.and.returnValue(of([{ id: 'r1', name: 'SOC Range A', state: 'ready' }]));

    await TestBed.configureTestingModule({
      imports: [MaintenanceWindowsComponent, NoopAnimationsModule],
      providers: [
        { provide: MaintenanceWindowsApiService, useValue: api },
        { provide: ApiService, useValue: core },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { isPlatformAdmin: signal(false) } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(MaintenanceWindowsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  });

  const buttons = (text: string) =>
    Array.from(el.querySelectorAll('button')).filter(b => b.textContent?.includes(text)) as HTMLButtonElement[];

  it('lists windows with what they cover and whether they are in effect', () => {
    expect(el.textContent).toContain('vCenter upgrade');
    expect(el.textContent).toContain('SOC Range A');
    expect(el.textContent).toContain('In effect');
    expect(el.textContent).toContain('all organisations');
  });

  it('offers Edit and Delete only where the API says the caller may', () => {
    expect(buttons('Edit').length).toBe(1);
    expect(buttons('Delete').length).toBe(1);
  });

  it('deletes only after a second, explicit click', () => {
    buttons('Delete')[0].click();
    fixture.detectChanges();
    expect(api.remove).not.toHaveBeenCalled();
    buttons('Delete now')[0].click();
    expect(api.remove).toHaveBeenCalledWith('w1');
  });

  it('creates a window and shows the API refusal as such', () => {
    component.startCreate();
    component.form.update(f => ({ ...f!, reason: 'Storage firmware' }));
    api.create.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 403, error: { detail: 'Only a platform administrator can create a platform-wide maintenance window' } })));
    component.save();
    expect(api.create).toHaveBeenCalled();
    expect(component.formError()).toContain('platform administrator');
    api.create.and.returnValue(of(WINDOW));
    component.save();
    expect(component.form()).toBeNull();
    expect(notify.success).toHaveBeenCalled();
  });

  it('does not offer platform-wide to an admin who is not the platform administrator', () => {
    component.startCreate();
    fixture.detectChanges();
    expect(el.textContent).not.toContain('platform-wide');
  });
});
