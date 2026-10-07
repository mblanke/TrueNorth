import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { SchedulerApiService } from '@core/services/scheduler-api.service';
import { MySessionsComponent } from './my-sessions.component';

describe('MySessionsComponent', () => {
  let fixture: ComponentFixture<MySessionsComponent>;
  let scheduler: jasmine.SpyObj<SchedulerApiService>;
  const text = () => fixture.nativeElement.textContent as string;
  const button = (label: string) =>
    Array.from(fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>).find(b => b.textContent!.trim() === label);

  beforeEach(async () => {
    scheduler = jasmine.createSpyObj('SchedulerApiService', ['mine', 'feedStatus', 'issueFeed', 'revokeFeed']);
    scheduler.mine.and.returnValue(of([
      { id: '1', name: 'SOC analyst lab', description: null, state: 'scheduled', start_time: '2026-10-15T13:00:00Z', end_time: '2026-10-15T16:00:00Z' },
    ]));
    scheduler.feedStatus.and.returnValue(of({ active: false, issued_at: null }));
    scheduler.issueFeed.and.returnValue(of({ url: 'https://h/api/v1/schedule/feed/T.ics', webcal_url: 'webcal://h/api/v1/schedule/feed/T.ics', issued_at: '' }));
    scheduler.revokeFeed.and.returnValue(of(void 0));
    await TestBed.configureTestingModule({
      imports: [MySessionsComponent, NoopAnimationsModule],
      providers: [{ provide: SchedulerApiService, useValue: scheduler }],
    }).compileComponents();
    fixture = TestBed.createComponent(MySessionsComponent);
    fixture.detectChanges();
  });

  it('lists the Student’s own sessions', () => {
    expect(text()).toContain('SOC analyst lab');
  });

  it('adds them to the Student’s calendar with a link shown once', () => {
    button('Add to my calendar')!.click();
    fixture.detectChanges();
    expect(text()).toContain('https://h/api/v1/schedule/feed/T.ics');
    button('Done')!.click();
    fixture.detectChanges();
    expect(text()).not.toContain('schedule/feed/T.ics');
    button('Turn off')!.click();
    expect(scheduler.revokeFeed).toHaveBeenCalled();
  });
});
