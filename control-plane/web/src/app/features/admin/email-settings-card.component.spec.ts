import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { EmailStatus } from '@core/services/email-status.service';
import { EmailSettingsCardComponent } from './email-settings-card.component';

const RELAY: EmailStatus = {
  configured: true, docs: 'x', can_send_test: true,
  relay: { host: 'smtp.corp.example', port: 587, security: 'starttls', from_address: 'tn@corp.example',
           username: 'tn-relay', signs_in: true },
};

describe('EmailSettingsCardComponent (#306)', () => {
  let http: HttpTestingController;

  function render(status: EmailStatus) {
    TestBed.configureTestingModule({
      imports: [EmailSettingsCardComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(EmailSettingsCardComponent);
    fixture.detectChanges();
    http.expectOne('/api/email/status').flush(status);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => http.verify());

  it('shows the platform administrator the relay and sends a test to their own address', () => {
    const fixture = render(RELAY);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('smtp.corp.example:587 (starttls)');
    expect(el.textContent).toContain('tn@corp.example');
    const button = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Send test email'))!;
    button.click();
    const req = http.expectOne('/api/email/test');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});  // no recipient: the API sends to the caller
    req.flush({ sent: true, to: 'me@corp.example', detail: 'The relay accepted the message for me@corp.example.' });
    fixture.detectChanges();
    expect(el.querySelector('.result')?.textContent).toContain('accepted the message');
  });

  it("says when a test is refused, in the API's words", () => {
    const fixture = render(RELAY);
    const el: HTMLElement = fixture.nativeElement;
    (Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Send test'))!).click();
    http.expectOne('/api/email/test').flush({ detail: 'A test email was sent less than a minute ago.' },
      { status: 429, statusText: 'Too Many Requests' });
    fixture.detectChanges();
    expect(el.querySelector('.result')?.textContent).toContain('less than a minute ago');
  });

  it('offers no test, and points at the install docs, while email is off', () => {
    const fixture = render({ configured: false, docs: 'x', relay: null, can_send_test: false });
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Not configured');
    expect(el.querySelector('a')?.getAttribute('href')).toContain('install/README.md#email-smtp');
    expect(Array.from(el.querySelectorAll('button')).some(b => b.textContent?.includes('Send test'))).toBeFalse();
  });

  it('shows no relay to an admin the API gives none', () => {
    const fixture = render({ configured: true, docs: 'x', relay: null, can_send_test: false });
    expect((fixture.nativeElement as HTMLElement).querySelector('.relay')).toBeNull();
  });
});
