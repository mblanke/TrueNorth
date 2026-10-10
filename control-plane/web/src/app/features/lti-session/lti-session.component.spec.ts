import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';

import { clearLtiSession, handoffCode, hasLtiSession, ltiSessionToken, storeLtiSession } from '@core/auth/lti-session';
import { ApiService, LtiSession } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { LtiSessionComponent } from './lti-session.component';

describe('LtiSessionComponent (LMS launch hand-off)', () => {
  const CODE = 'Abc_def-1234567890XYZ';
  let api: jasmine.SpyObj<ApiService>;
  let auth: jasmine.SpyObj<AuthService>;
  let router: jasmine.SpyObj<Router>;

  const session = (target: string): LtiSession => ({
    access_token: 'lti.jwt', token_type: 'Bearer', expires_in: 7200, target,
    user: { id: 'u1', display_name: 'S', role: 'student' },
  });

  async function land(fragment: string | null, answer: Observable<LtiSession>): Promise<LtiSessionComponent> {
    api = jasmine.createSpyObj('ApiService', ['health', 'exchangeLtiSession']);
    api.health.and.returnValue(of({ status: 'ok', version: 'test' }));
    api.exchangeLtiSession.and.returnValue(answer);
    auth = jasmine.createSpyObj('AuthService', ['bootstrap']);
    auth.bootstrap.and.resolveTo(null);
    router = jasmine.createSpyObj('Router', ['navigateByUrl']);
    router.navigateByUrl.and.resolveTo(true);
    TestBed.configureTestingModule({
      imports: [LtiSessionComponent],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: Router, useValue: router },
        { provide: ActivatedRoute, useValue: { snapshot: { fragment } } },
      ],
    });
    replaced = spyOn(history, 'replaceState');
    const component = TestBed.createComponent(LtiSessionComponent).componentInstance;
    await component.ngOnInit();
    return component;
  }
  let replaced: jasmine.Spy;

  afterEach(() => clearLtiSession());

  it('exchanges the code once, keeps the session, and goes on to the launched quiz', async () => {
    await land(`code=${CODE}`, of(session('/quiz-player?quiz=q1&lti=1')));
    expect(replaced).toHaveBeenCalled();  // the code leaves the address bar first
    expect(replaced.calls.mostRecent().args[2]).not.toContain(CODE);
    expect(api.exchangeLtiSession).toHaveBeenCalledOnceWith(CODE);
    expect(api.health).toHaveBeenCalledBefore(api.exchangeLtiSession);  // CSRF cookie first
    expect(ltiSessionToken()).toBe('lti.jwt');
    expect(auth.bootstrap).toHaveBeenCalledWith(true);
    expect(router.navigateByUrl).toHaveBeenCalledWith('/quiz-player?quiz=q1&lti=1', { replaceUrl: true });
  });

  it('never follows a target off this site', async () => {
    await land(`code=${CODE}`, of(session('//evil.example/x')));
    expect(router.navigateByUrl).toHaveBeenCalledWith('/dashboard', { replaceUrl: true });
  });

  it('explains a spent, expired or other-browser code and keeps no session', async () => {
    const component = await land(`code=${CODE}`, throwError(() => ({ status: 401 })));
    expect(component.error()).toContain('another browser');
    expect(hasLtiSession()).toBeFalse();
    expect(router.navigateByUrl).not.toHaveBeenCalled();
  });

  it('refuses a link with no code without calling the API', async () => {
    const component = await land(null, of(session('/')));
    expect(component.error()).toContain('no sign-in code');
    expect(api.exchangeLtiSession).not.toHaveBeenCalled();
  });
});

describe('lti-session store', () => {
  afterEach(() => clearLtiSession());

  it('keeps a token only until it expires', () => {
    storeLtiSession('tok', 7200);
    expect(ltiSessionToken()).toBe('tok');
    storeLtiSession('old', 0);
    expect(ltiSessionToken()).toBe('');
    expect(hasLtiSession()).toBeFalse();
  });

  it('reads only a well-formed code from the fragment', () => {
    expect(handoffCode('#code=Abc_def-1234567890XYZ')).toBe('Abc_def-1234567890XYZ');
    expect(handoffCode('#code=<script>')).toBeNull();
    expect(handoffCode('#token=Abc_def-1234567890XYZ')).toBeNull();
    expect(handoffCode('')).toBeNull();
  });
});
