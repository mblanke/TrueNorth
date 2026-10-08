import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { HealthResponse, Tenant } from '@core/models';
import { AdminComponent } from './admin.component';

describe('AdminComponent', () => {
  let fixture: ComponentFixture<AdminComponent>;
  let component: AdminComponent;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  const tenant = { id: 't1', name: 'Default Org', slug: 'default' } as Tenant;

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', [
      'health', 'listTenants', 'listAuditLog', 'createTenant', 'updateTenant',
    ]);
    api.health.and.returnValue(of({ status: 'ok' } as HealthResponse));
    api.listTenants.and.returnValue(of([tenant]));
    api.listAuditLog.and.returnValue(throwError(() => new Error('403')));
    api.createTenant.and.returnValue(of({ id: 't2', name: 'Blue Cell', slug: 'blue' } as Tenant));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);

    await TestBed.configureTestingModule({
      imports: [AdminComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(AdminComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('builds Services links from the environment, never a hardcoded localhost Keycloak', () => {
    const names = component.serviceLinks.map((l) => l.name);
    expect(names[0]).toBe('Keycloak');
    expect(component.serviceLinks[0].url).toMatch(/\/admin\/$/);
    expect(component.serviceLinks.every((l) => /^(https?:\/\/|\/)/.test(l.url))).toBeTrue();
  });

  it('creates and loads health and tenants', () => {
    expect(component).toBeTruthy();
    expect(component.health()?.status).toBe('ok');
    expect(component.tenants()).toEqual([tenant]);
    // An audit log the caller may not read shows as empty rather than failing the page.
    expect(component.auditLog()).toEqual([]);
  });

  it('creates a tenant, reloads the list and closes the form', () => {
    component.showCreateTenant = true;
    component.tenantForm = { name: 'Blue Cell', slug: 'blue' };
    api.listTenants.calls.reset();

    component.createTenant();

    expect(api.createTenant).toHaveBeenCalledWith({ name: 'Blue Cell', slug: 'blue' });
    expect(notify.success).toHaveBeenCalledWith('Tenant created');
    expect(api.listTenants).toHaveBeenCalled();
    expect(component.showCreateTenant).toBeFalse();
    expect(component.tenantForm).toEqual({ name: '', slug: '' });
  });

  it('keeps the form open and reports a failed create', () => {
    api.createTenant.and.returnValue(throwError(() => new Error('409')));
    component.showCreateTenant = true;
    component.tenantForm = { name: 'Dup', slug: 'default' };

    component.createTenant();

    expect(notify.error).toHaveBeenCalled();
    expect(component.showCreateTenant).toBeTrue();
  });
});
