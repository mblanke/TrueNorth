import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute } from '@angular/router';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { LtiLinkComponent } from './lti-link.component';

describe('LtiLinkComponent (staff deep linking)', () => {
  const CODE = 'Link_code-1234567890abcd';
  let api: jasmine.SpyObj<ApiService>;
  let auth: {
    bootstrap: jasmine.Spy; login: jasmine.Spy; isAuthenticated: () => boolean; isLtiSession: () => boolean;
    user: () => CurrentUser | null;
  };

  async function open(fragment: string | null, signedIn = true, lti = false): Promise<LtiLinkComponent> {
    api = jasmine.createSpyObj('ApiService', ['previewLtiLink', 'confirmLtiLink']);
    api.previewLtiLink.and.returnValue(of({ platform_id: 'p', platform_name: 'Moodle (default)', lms_name: 'Ada' }));
    api.confirmLtiLink.and.returnValue(of({ id: 'l', platform_id: 'p', platform_name: 'Moodle (default)',
                                             lms_name: 'Ada', confirmed_at: null }));
    const me = signal<CurrentUser | null>(signedIn ? {
      sub: 'kc', id: 'u', email: 'ada@unit.test', display_name: 'Ada', role: 'instructor',
    } : null);
    auth = {
      bootstrap: jasmine.createSpy('bootstrap').and.resolveTo(null),
      login: jasmine.createSpy('login'),
      isAuthenticated: () => me() !== null,
      isLtiSession: () => lti,
      user: () => me(),
    };
    spyOn(history, 'replaceState');
    TestBed.configureTestingModule({
      imports: [LtiLinkComponent],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: ActivatedRoute, useValue: { snapshot: { fragment } } },
      ],
    });
    const component = TestBed.createComponent(LtiLinkComponent).componentInstance;
    await component.ngOnInit();
    return component;
  }

  afterEach(() => sessionStorage.removeItem('tn.lti-link-code'));

  it('shows what will be linked and links only when the staff member presses Link', async () => {
    const c = await open(`code=${CODE}`);
    expect(api.previewLtiLink).toHaveBeenCalledWith(CODE);
    expect(c.preview()?.platform_name).toBe('Moodle (default)');
    expect(api.confirmLtiLink).not.toHaveBeenCalled();
    await c.confirm();
    expect(api.confirmLtiLink).toHaveBeenCalledOnceWith(CODE);
    expect(c.done()).toBeTrue();
    expect(sessionStorage.getItem('tn.lti-link-code')).toBeNull();
  });

  it('sends someone not signed in to sign in, keeping the code in this tab only', async () => {
    await open(`code=${CODE}`, false);
    expect(auth.login).toHaveBeenCalledWith(`${window.location.origin}/lti/link`);
    expect((auth.login.calls.mostRecent().args[0] as string)).not.toContain(CODE);
    expect(sessionStorage.getItem('tn.lti-link-code')).toBe(CODE);
    expect(api.previewLtiLink).not.toHaveBeenCalled();
  });

  it('never links from an LTI session: staff sign in as themselves', async () => {
    await open(`code=${CODE}`, true, true);
    expect(auth.login).toHaveBeenCalled();
    expect(api.previewLtiLink).not.toHaveBeenCalled();
  });

  it('shows the API refusal (say, an email that is not yours)', async () => {
    const c = await open(`code=${CODE}`);
    api.previewLtiLink.and.returnValue(throwError(() => ({
      status: 403, error: { detail: 'The learning platform named a different email than your TrueNorth account\'s' },
    })));
    await c.ngOnInit();
    expect(c.error()).toContain('different email');
  });
});
