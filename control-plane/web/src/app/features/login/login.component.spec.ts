import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import { LoginComponent } from './login.component';
import { AuroraScene } from './aurora-scene';

describe('LoginComponent', () => {
  let auth: { isAuthenticated: jasmine.Spy; login: jasmine.Spy };
  let router: Router;

  function create(query: Record<string, string> = {}) {
    TestBed.configureTestingModule({
      imports: [LoginComponent],
      providers: [
        provideRouter([]),
        { provide: AuthService, useValue: auth },
        { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(query) } } },
      ],
    });
    router = TestBed.inject(Router);
    spyOn(router, 'navigate').and.resolveTo(true);
    spyOn(router, 'navigateByUrl').and.resolveTo(true);
    const fixture = TestBed.createComponent(LoginComponent);
    fixture.detectChanges();
    return fixture;
  }

  beforeEach(() => {
    auth = {
      isAuthenticated: jasmine.createSpy('isAuthenticated').and.returnValue(false),
      login: jasmine.createSpy('login'),
    };
    // The aurora background is WebGL (three.js); keep it out of a unit test.
    spyOn(AuroraScene, 'create').and.returnValue(Promise.reject(new Error('no WebGL in tests')));
  });

  it('renders the sign-in card for an anonymous visitor', () => {
    const fixture = create();
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button.sso-btn');
    expect(fixture.componentInstance).toBeTruthy();
    expect(button.textContent).toContain('Sign in with SSO');
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('starts the SSO sign-in when the button is pressed', () => {
    const fixture = create();
    (fixture.nativeElement.querySelector('button.sso-btn') as HTMLButtonElement).click();
    expect(auth.login).toHaveBeenCalledTimes(1);
  });

  it('sends a signed-in user to the dashboard', () => {
    auth.isAuthenticated.and.returnValue(true);
    create();
    expect(router.navigate).toHaveBeenCalledWith(['/dashboard']);
  });

  it('passes a safe returnUrl to SSO so the Student comes back to their lab', () => {
    const fixture = create({ returnUrl: '/labs/lab-42' });
    (fixture.nativeElement.querySelector('button.sso-btn') as HTMLButtonElement).click();
    expect(auth.login).toHaveBeenCalledWith(`${window.location.origin}/labs/lab-42`);
  });

  it('sends a signed-in user straight to a safe returnUrl', () => {
    auth.isAuthenticated.and.returnValue(true);
    create({ returnUrl: '/labs/lab-42' });
    expect(router.navigateByUrl).toHaveBeenCalledWith('/labs/lab-42');
    expect(router.navigate).not.toHaveBeenCalled();
  });

  for (const hostile of ['https://evil.test/labs/1', '//evil.test', '/\\evil.test', 'javascript:alert(1)']) {
    it(`ignores an unsafe returnUrl (${hostile})`, () => {
      const fixture = create({ returnUrl: hostile });
      (fixture.nativeElement.querySelector('button.sso-btn') as HTMLButtonElement).click();
      expect(auth.login).toHaveBeenCalledWith(undefined);
    });

    it(`sends a signed-in user to the dashboard, not to ${hostile}`, () => {
      auth.isAuthenticated.and.returnValue(true);
      create({ returnUrl: hostile });
      expect(router.navigate).toHaveBeenCalledWith(['/dashboard']);
      expect(router.navigateByUrl).not.toHaveBeenCalled();
    });
  }

  it('stays put for a signed-in user with ?preview', () => {
    auth.isAuthenticated.and.returnValue(true);
    create({ preview: '1' });
    expect(router.navigate).not.toHaveBeenCalled();
  });
});
