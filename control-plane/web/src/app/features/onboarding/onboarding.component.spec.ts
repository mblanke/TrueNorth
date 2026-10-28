import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { OnboardingState, RegistrationApiService } from '@core/services/registration-api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { TourService } from '../../shared/tour/tour.service';
import { OnboardingComponent } from './onboarding.component';

/** The first-run wizard: a Student admitted from a roll call made no access request (ADR 0014 §1, #319). */
describe('OnboardingComponent', () => {
  let fixture: ComponentFixture<OnboardingComponent>;

  function setup(state: Partial<OnboardingState>) {
    const registration = jasmine.createSpyObj('RegistrationApiService',
      ['onboardingState', 'nations', 'qualifications', 'saveOnboardingProfile', 'selectOnboardingPath',
        'completeOnboarding', 'skipOnboarding']);
    registration.onboardingState.and.returnValue(of({ state: 'not_started', ...state } as OnboardingState));
    registration.nations.and.returnValue(of([]));
    registration.qualifications.and.returnValue(of([]));
    TestBed.configureTestingModule({
      imports: [OnboardingComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: RegistrationApiService, useValue: registration },
        { provide: AuthService, useValue: { bootstrap: () => Promise.resolve() } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['error']) },
        { provide: TourService, useValue: jasmine.createSpyObj('TourService', ['start']) },
      ],
    });
    fixture = TestBed.createComponent(OnboardingComponent);
    fixture.detectChanges();
  }
  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';

  it('asks someone who requested access to confirm their details', () => {
    setup({ via_roll_call: false });
    expect(text()).toContain('Confirm your details');
    expect(text()).toContain('Carried over from your access request');
  });

  it('skips the access-request step for a Student admitted from a roll call', () => {
    setup({ via_roll_call: true });
    expect(text()).not.toContain('Confirm your details');
    expect(text()).not.toContain('access request');
    expect(text()).toContain('already enrolled on its course');
    expect(text()).toContain('Where things are');
  });
});
