import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { HealthResponse, Tenant } from '@core/models';
import { AdminComponent } from './admin.component';
import { EmailStatusService } from '@core/services/email-status.service';
import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { AuthService } from '@core/services/auth.service';

describe('AdminComponent', () => {
  let fixture: ComponentFixture<AdminComponent>;
  let component: AdminComponent;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  const platformAdmin = signal(true);

  const tenant = { id: 't1', name: 'Default Org', slug: 'default' } as Tenant;

  beforeEach(async () => {
    platformAdmin.set(true);
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
        // The card's own spec covers it (email-settings-card.component.spec.ts).
        { provide: EmailStatusService, useValue: { load: () => undefined, status: signal(null) } },
        { provide: AuthService, useValue: { isPlatformAdmin: platformAdmin } },
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

  it('keeps the form open and reports why a create failed', () => {
    api.createTenant.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: "Tenant slug 'default' already exists" },
    })));
    component.showCreateTenant = true;
    component.tenantForm = { name: 'Dup', slug: 'default' };

    component.createTenant();

    expect(notify.error).toHaveBeenCalledWith("Could not create the tenant: Tenant slug 'default' already exists");
    expect(component.showCreateTenant).toBeTrue();
  });

  async function tenantsTab(): Promise<HTMLElement> {
    const tabs = Array.from(fixture.nativeElement.querySelectorAll('[role="tab"]')) as HTMLElement[];
    tabs.find(t => t.textContent?.includes('Tenants'))!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows New Tenant to the platform administrator', async () => {
    const el = await tenantsTab();
    expect(component.canCreateTenant()).toBeTrue();
    expect(el.textContent).toContain('New Tenant');
  });

  it('hides New Tenant from an admin who is not the platform administrator, and says why', async () => {
    platformAdmin.set(false);
    const el = await tenantsTab();
    expect(component.canCreateTenant()).toBeFalse();
    expect(el.textContent).not.toContain('New Tenant');
    expect(el.textContent).toContain('Only the platform administrator creates tenants');
  });
});
