import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { environment } from '@env/environment';

import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { RegisterComponent } from './register.component';

const API = environment.apiUrl;

/**
 * rc12: the Nation dropdown on "Request access" was empty because it read
 * /directory/nations (user:read), which an applicant does not hold. It now reads
 * /registration/nations, which any valid token may call. The Qualification dropdown had
 * the same fault (/qsp/qualifications needs a users row) and now reads
 * /registration/qualifications.
 */
describe('RegisterComponent', () => {
  let fixture: ComponentFixture<RegisterComponent>;
  let component: RegisterComponent;
  let http: HttpTestingController;

  const NATIONS = [
    { id: 'n-1', name: 'Canada', iso_alpha2: 'CA', iso_alpha3: 'CAN', flag_emoji: '🇨🇦' },
    { id: 'n-2', name: 'Norway', iso_alpha2: 'NO', iso_alpha3: 'NOR', flag_emoji: '🇳🇴' },
  ];
  const QUALS = [{ id: 'q-1', qsp_code: 'ALJQ', title: 'Cyber Operator' }];

  beforeEach(async () => {
    const auth = jasmine.createSpyObj('AuthService', ['bootstrap']);
    auth.bootstrap.and.resolveTo({ status: 'unregistered', prefill: null, suggestions: null });
    await TestBed.configureTestingModule({
      imports: [RegisterComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: AuthService, useValue: auth },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(RegisterComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => http.verify());

  it('tells someone who lost their seat on a class to ask their instructor', async () => {
    const auth = TestBed.inject(AuthService) as jasmine.SpyObj<AuthService>;
    auth.bootstrap.and.resolveTo({ status: 'unregistered', roll_call_forfeited: ['C201-26-03'] });
    await init();
    http.expectOne(`${API}/registration/nations`).flush([]);
    http.expectOne(`${API}/registration/qualifications`).flush([]);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(component.forfeited()).toEqual(['C201-26-03']);
    expect(text).toContain('You lost your seat on C201-26-03');
    expect(text).toContain('Ask your instructor');
  });

  async function init(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  it('fills the Nation dropdown from /registration/nations, never /directory/nations', async () => {
    await init();

    http.expectNone(`${API}/directory/nations`);
    http.expectOne({ method: 'GET', url: `${API}/registration/nations` }).flush(NATIONS);
    http.expectOne(`${API}/registration/qualifications`).flush([]);

    expect(component.nations().map(n => n.name)).toEqual(['Canada', 'Norway']);
  });

  it('fills the Qualification dropdown from /registration/qualifications, never /qsp', async () => {
    await init();

    http.expectNone(`${API}/qsp/qualifications`);
    http.expectOne(`${API}/registration/nations`).flush([]);
    http.expectOne({ method: 'GET', url: `${API}/registration/qualifications` }).flush(QUALS);

    expect(component.qualifications().map(q => q.qsp_code)).toEqual(['ALJQ']);
  });

  it('leaves the list empty, and the form usable, when the reference calls fail', async () => {
    await init();

    http.expectOne(`${API}/registration/nations`).flush('nope', { status: 500, statusText: 'Server Error' });
    http.expectOne(`${API}/registration/qualifications`).flush('nope', { status: 500, statusText: 'Server Error' });

    expect(component.nations()).toEqual([]);
    expect(component.qualifications()).toEqual([]);
    expect(component.loading()).toBeFalse();
  });
});
