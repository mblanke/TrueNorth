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

  it('stays put for a signed-in user with ?preview', () => {
    auth.isAuthenticated.and.returnValue(true);
    create({ preview: '1' });
    expect(router.navigate).not.toHaveBeenCalled();
  });
});
