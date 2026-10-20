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
 * /registration/nations, which any valid token may call.
 */
describe('RegisterComponent', () => {
  let fixture: ComponentFixture<RegisterComponent>;
  let component: RegisterComponent;
  let http: HttpTestingController;

  const NATIONS = [
    { id: 'n-1', name: 'Canada', iso_alpha2: 'CA', iso_alpha3: 'CAN', flag_emoji: '🇨🇦' },
    { id: 'n-2', name: 'Norway', iso_alpha2: 'NO', iso_alpha3: 'NOR', flag_emoji: '🇳🇴' },
  ];

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

  async function init(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  it('fills the Nation dropdown from /registration/nations, never /directory/nations', async () => {
    await init();

    http.expectNone(`${API}/directory/nations`);
    http.expectOne({ method: 'GET', url: `${API}/registration/nations` }).flush(NATIONS);
    http.expectOne(`${API}/qsp/qualifications`).flush([]);

    expect(component.nations().map(n => n.name)).toEqual(['Canada', 'Norway']);
  });

  it('leaves the list empty, and the form usable, when the call fails', async () => {
    await init();

    http.expectOne(`${API}/registration/nations`).flush('nope', { status: 500, statusText: 'Server Error' });
    http.expectOne(`${API}/qsp/qualifications`).flush([]);

    expect(component.nations()).toEqual([]);
    expect(component.loading()).toBeFalse();
  });
});
