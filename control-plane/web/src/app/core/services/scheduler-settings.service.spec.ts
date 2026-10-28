import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { SchedulerSettingsService, durationWords } from './scheduler-settings.service';

describe('SchedulerSettingsService (#318)', () => {
  let http: HttpTestingController;
  let service: SchedulerSettingsService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(SchedulerSettingsService);
  });

  afterEach(() => http.verify());

  it('says the site’s lead and horizon once the server answers, and asks only once', () => {
    expect(service.leadPhrase()).toBe('before');
    expect(service.nextFreeHorizon()).toBe('soon');
    service.load();
    service.load();
    http.expectOne('/api/schedule/settings').flush({
      lead_minutes: 45, grace_minutes: 15, next_free_days: 7, site_timezone: 'America/Toronto',
    });
    expect(service.leadPhrase()).toBe('45 minutes before');
    expect(service.nextFreeHorizon()).toBe('in the next 7 days');
    service.load();
    http.expectNone('/api/schedule/settings');
  });

  it('leaves the number out when the caller may not read the schedule', () => {
    service.load();
    http.expectOne('/api/schedule/settings').flush('no', { status: 403, statusText: 'Forbidden' });
    expect(service.leadPhrase()).toBe('before');
  });

  it('puts durations in words', () => {
    expect(durationWords(30)).toBe('30 minutes');
    expect(durationWords(60)).toBe('1 hour');
    expect(durationWords(90)).toBe('90 minutes');
    expect(durationWords(120)).toBe('2 hours');
    expect(durationWords(1)).toBe('1 minute');
  });
});
