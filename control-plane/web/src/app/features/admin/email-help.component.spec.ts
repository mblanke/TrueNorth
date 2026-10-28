import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { EmailHelpComponent } from './email-help.component';

describe('EmailHelpComponent (#318)', () => {
  it('explains the installer settings without linking outside the app', () => {
    TestBed.configureTestingModule({ imports: [EmailHelpComponent], providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(EmailHelpComponent);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    for (const v of ['tn_smtp_host', 'tn_smtp_port', 'tn_smtp_security', 'tn_smtp_username',
                     'vault_smtp_password', 'tn_smtp_from_address', '30-config.yml', '50-stack-up.yml']) {
      expect(el.textContent).toContain(v);
    }
    // An air-gapped site reaches nothing outside: every link stays in the app.
    for (const a of Array.from(el.querySelectorAll('a'))) {
      expect(a.getAttribute('href')).toMatch(/^\//);
    }
  });
});
