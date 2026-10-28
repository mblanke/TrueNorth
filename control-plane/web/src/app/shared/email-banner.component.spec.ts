import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { AuthService, UserRole } from '@core/services/auth.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { EMAIL_DOCS_URL } from '@core/services/email-status.service';
import { EmailBannerComponent } from './email-banner.component';
import { provideRouter } from '@angular/router';

describe('EmailBannerComponent (#306)', () => {
  let http: HttpTestingController;
  let auth: ReturnType<typeof fakeAuth>;

  function render(role: UserRole | null) {
    auth = fakeAuth(role);
    TestBed.configureTestingModule({
      imports: [EmailBannerComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]), { provide: AuthService, useValue: auth }],
    });
    http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(EmailBannerComponent);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => http.verify());

  it('tells an admin, persistently, that email is not configured, with a link to the bundled help', () => {
    const fixture = render('admin');
    http.expectOne('/api/email/status').flush({ configured: false, docs: 'x', relay: null, can_send_test: false });
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const banner = el.querySelector('.email-banner');
    expect(banner?.textContent).toContain(
      'Email is not configured: onboarding emails, reminders and calendar invites are not being sent');
    expect(banner?.getAttribute('role')).toBe('alert');
    const link = banner?.querySelector('a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(EMAIL_DOCS_URL);
    // Inside the app: an air-gapped site cannot reach GitHub (#318).
    expect(EMAIL_DOCS_URL).toBe('/admin/help/email');
    expect(link.target).toBe('');
    // Persistent: nothing to dismiss it with.
    expect(banner?.querySelector('button')).toBeNull();
  });

  it('shows nothing once email is configured', () => {
    const fixture = render('admin');
    http.expectOne('/api/email/status').flush({ configured: true, docs: 'x', relay: null, can_send_test: true });
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.email-banner')).toBeNull();
  });

  it('shows nothing while the server has not answered, or when the check fails', () => {
    const fixture = render('admin');
    expect((fixture.nativeElement as HTMLElement).querySelector('.email-banner')).toBeNull();
    http.expectOne('/api/email/status').flush('down', { status: 502, statusText: 'Bad Gateway' });
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.email-banner')).toBeNull();
  });

  for (const role of ['instructor', 'student', 'observer', 'range_ops'] as UserRole[]) {
    it(`is not shown to, and not fetched for, a ${role}`, () => {
      const fixture = render(role);
      http.expectNone('/api/email/status');
      expect((fixture.nativeElement as HTMLElement).querySelector('.email-banner')).toBeNull();
    });
  }

  it('asks once the signed-in admin is known', () => {
    const fixture = render(null);
    http.expectNone('/api/email/status');
    auth.role.set('admin');
    fixture.detectChanges();
    http.expectOne('/api/email/status').flush({ configured: false, docs: 'x', relay: null, can_send_test: false });
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.email-banner')).not.toBeNull();
  });
});
