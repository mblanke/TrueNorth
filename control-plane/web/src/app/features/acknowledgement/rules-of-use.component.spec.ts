import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { AcknowledgementApiService, AcknowledgementCurrent } from '@core/services/acknowledgement-api.service';
import { RulesOfUseComponent } from './rules-of-use.component';

const VERSION = {
  id: 'v1', version: 3, published_at: '2026-10-01T00:00:00Z', published_by: 'u0',
  text: '# Rules\n\nSessions may be observed.\n\n<script>window.pwned = true</script><img src="https://x.test/b.png">',
} as AcknowledgementCurrent['version'];

function current(over: Partial<AcknowledgementCurrent> = {}): AcknowledgementCurrent {
  return { version: VERSION, required: true, reason: 'never_signed', signed_at: null, expires_at: null, ...over };
}

describe('RulesOfUseComponent', () => {
  let fixture: ComponentFixture<RulesOfUseComponent>;
  let component: RulesOfUseComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<AcknowledgementApiService>;
  let auth: jasmine.SpyObj<AuthService>;
  let router: jasmine.SpyObj<Router>;

  function setup(first: AcknowledgementCurrent = current(), returnUrl: string | null = '/scoring?exercise=e1') {
    api = jasmine.createSpyObj('AcknowledgementApiService', ['current', 'sign']);
    api.current.and.returnValue(of(first));
    api.sign.and.returnValue(of(current({ required: false, reason: 'signed', signed_at: '2026-10-23T10:00:00Z',
      expires_at: '2027-10-23T10:00:00Z' })));
    auth = jasmine.createSpyObj('AuthService', ['setAcknowledgementRequired', 'logout']);
    router = jasmine.createSpyObj('Router', ['navigateByUrl']);
    router.navigateByUrl.and.resolveTo(true);
    TestBed.configureTestingModule({
      imports: [RulesOfUseComponent, NoopAnimationsModule],
      providers: [
        { provide: AcknowledgementApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: Router, useValue: router },
        { provide: ActivatedRoute,
          useValue: { snapshot: { queryParamMap: convertToParamMap(returnUrl ? { returnUrl } : {}) } } },
      ],
    });
    fixture = TestBed.createComponent(RulesOfUseComponent);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  const agreeButton = () =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('I agree')) as HTMLButtonElement;

  it('renders the Markdown, sanitised', () => {
    setup();
    expect(el.querySelector('h1')?.textContent).toContain('Rules of use');
    expect(el.textContent).toContain('Version 3');
    expect(el.querySelector('.tn-md h1')?.textContent).toContain('Rules');
    expect(el.textContent).toContain('Sessions may be observed');
    expect(el.querySelector('.tn-md script')).toBeNull();
    expect(el.querySelector('.tn-md img')?.getAttribute('src')).toBeNull();
    expect((window as unknown as { pwned?: boolean }).pwned).toBeUndefined();
    expect(auth.setAcknowledgementRequired).toHaveBeenCalledWith(true);
  });

  it('enables "I agree" only once the text has been read to the end', () => {
    setup();
    component.scrolledToEnd.set(false);
    fixture.detectChanges();
    expect(agreeButton().disabled).toBeTrue();
    expect(el.textContent).toContain('Scroll to the end');
    component.scrolledToEnd.set(true);
    fixture.detectChanges();
    expect(agreeButton().disabled).toBeFalse();
  });

  it('never strands anyone: text that renders blank still enables "I agree" once rendered', async () => {
    setup(current({ version: { ...VERSION!, text: '<!-- nothing to read -->' } }));
    await new Promise(resolve => setTimeout(resolve));
    fixture.detectChanges();
    expect(agreeButton().disabled).toBeFalse();
  });

  it('short text that needs no scrolling enables "I agree" once rendered', async () => {
    setup(current({ version: { ...VERSION!, text: 'Be good.' } }));
    await new Promise(resolve => setTimeout(resolve));
    fixture.detectChanges();
    expect(agreeButton().disabled).toBeFalse();
  });

  it('signs the version shown and goes back where the user was going', () => {
    setup();
    component.scrolledToEnd.set(true);
    fixture.detectChanges();
    agreeButton().click();
    expect(api.sign).toHaveBeenCalledWith('v1');
    expect(auth.setAcknowledgementRequired).toHaveBeenCalledWith(false);
    expect(router.navigateByUrl).toHaveBeenCalledWith('/scoring?exercise=e1');
  });

  it('never follows an off-site returnUrl', () => {
    setup(current(), 'https://evil.test/x');
    component.scrolledToEnd.set(true);
    fixture.detectChanges();
    agreeButton().click();
    expect(router.navigateByUrl).toHaveBeenCalledWith('/dashboard');
  });

  it('shows the newer version when one was published while reading (409)', () => {
    setup();
    api.sign.and.returnValue(throwError(() => new HttpErrorResponse({ status: 409 })));
    component.scrolledToEnd.set(true);
    fixture.detectChanges();
    agreeButton().click();
    expect(api.current).toHaveBeenCalledTimes(2);
    expect(router.navigateByUrl).not.toHaveBeenCalled();
  });

  it('explains a new version and a yearly re-sign', () => {
    setup(current({ reason: 'new_version' }));
    expect(el.textContent).toContain('have changed');
    TestBed.resetTestingModule();
    setup(current({ reason: 'expired' }));
    expect(el.textContent).toContain('a year');
  });

  it('lets someone who has nothing to sign carry on', () => {
    setup(current({ version: null, required: false, reason: 'none_published' }), null);
    expect(agreeButton()).toBeUndefined();
    (Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Continue')) as HTMLButtonElement)
      .click();
    expect(router.navigateByUrl).toHaveBeenCalledWith('/dashboard');
  });

  it('offers sign-out instead of agreeing', () => {
    setup();
    (Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Sign out')) as HTMLButtonElement)
      .click();
    expect(auth.logout).toHaveBeenCalled();
    expect(api.sign).not.toHaveBeenCalled();
  });
});
