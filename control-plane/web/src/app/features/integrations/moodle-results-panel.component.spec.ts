import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { NotificationService } from '@core/services/notification.service';
import { MoodleResultsStatus, OperatorRecordsApiService } from '@core/services/operator-records-api.service';
import { MoodleResultsPanelComponent, describePull } from './moodle-results-panel.component';

const FRESH: MoodleResultsStatus = {
  platform_id: 'p1', cursor: '12', last_run_at: '2026-10-12T09:00:00Z', last_success_at: '2026-10-12T09:00:00Z',
  last_error: '', rows_seen: 12, rows_applied: 10, running: false, scheduled_every_seconds: 600, stale: false,
  stale_reason: '',
};
const STALE: MoodleResultsStatus = {
  ...FRESH, stale: true, stale_reason: 'The last successful pull is older than two scheduled intervals (1200 s).',
  last_error: 'signature did not verify',
};

describe('MoodleResultsPanelComponent', () => {
  let fixture: ComponentFixture<MoodleResultsPanelComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<OperatorRecordsApiService>;
  let dialogOpen: jasmine.Spy;

  async function render(status: unknown, canPull = true) {
    api = jasmine.createSpyObj('OperatorRecordsApiService', ['getMoodleResults', 'pullMoodleResults']);
    api.getMoodleResults.and.returnValue(status as any);
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of({ reason: '' }) }));
    await TestBed.configureTestingModule({
      imports: [MoodleResultsPanelComponent, NoopAnimationsModule],
      providers: [
        { provide: OperatorRecordsApiService, useValue: api },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(MoodleResultsPanelComponent);
    fixture.componentRef.setInput('platformId', 'p1');
    fixture.componentRef.setInput('platformName', 'Moodle LMS');
    fixture.componentRef.setInput('canPull', canPull);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const pullButton = () =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Pull')) as HTMLButtonElement | undefined;

  it('shows the last sync, schedule and an up-to-date state', async () => {
    await render(of(FRESH));
    expect(api.getMoodleResults).toHaveBeenCalledWith('p1');
    expect(el.textContent).toContain('Up to date');
    expect(el.textContent).toContain('every 10 min');
    expect(el.textContent).toContain('12 / 10');
  });

  it('shows stale with the API\'s reason and the last error', async () => {
    await render(of(STALE));
    expect(el.textContent).toContain('Stale');
    expect(el.textContent).toContain('older than two scheduled intervals');
    expect(el.textContent).toContain('signature did not verify');
  });

  it('pulls now after the impact dialog and reports what was recorded', async () => {
    await render(of(FRESH));
    api.pullMoodleResults.and.returnValue(of({ platform_id: 'p1', cursor: '14', pages: 1, rows: 2, applied: 1,
      unchanged: 1, skipped: {}, more: false }));
    pullButton()!.click();
    expect(dialogOpen).toHaveBeenCalled();
    expect(api.pullMoodleResults).toHaveBeenCalledWith('p1', false);
    fixture.detectChanges();
    expect(el.textContent).toContain('Read 2 row(s) in 1 page(s): 1 recorded, 1 unchanged.');
    expect(api.getMoodleResults).toHaveBeenCalledTimes(2);
  });

  it('shows a 502 from the Moodle as a failed pull', async () => {
    await render(of(FRESH));
    api.pullMoodleResults.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 502, error: { detail: 'The Moodle answer was not signed' } })));
    pullButton()!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Pull failed: The Moodle answer was not signed');
  });

  it('no pull button without the permission, and none while a pull runs', async () => {
    await render(of(FRESH), false);
    expect(pullButton()).toBeUndefined();
    TestBed.resetTestingModule();
    await render(of({ ...FRESH, running: true }));
    expect(el.textContent).toContain('Pull running');
    expect(pullButton()!.disabled).toBeTrue();
  });

  it('shows a load error, not an empty status', async () => {
    await render(throwError(() => new HttpErrorResponse({ status: 422, error: { detail: 'This platform does not report Moodle results' } })));
    expect(el.textContent).toContain('does not report Moodle results');
  });

  it('describes skipped rows and more pages', () => {
    expect(describePull({ platform_id: 'p', cursor: '', pages: 2, rows: 3, applied: 1, unchanged: 0,
      skipped: { malformed: 2 }, more: true })).toContain('skipped 2 malformed. More remain');
  });
});
