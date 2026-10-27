import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import {
  AdStatusSyncOverview, AdStatusSyncRun, DirectoryApiService,
} from '@core/services/directory-api.service';
import { AdStatusSyncPanelComponent } from './ad-status-sync-panel.component';

describe('AdStatusSyncPanelComponent', () => {
  let fixture: ComponentFixture<AdStatusSyncPanelComponent>;
  let directory: jasmine.SpyObj<DirectoryApiService>;

  const run: AdStatusSyncRun = {
    id: 'r1', trigger: 'nightly', dry_run: true, status: 'completed', started_at: '2026-10-27T02:00:00Z',
    finished_at: '2026-10-27T02:00:05Z', error: null, counts: { examined: 12, ad_disabled: 1 },
    results: [{
      user_id: 'u1', email: 'ann@corp.test', display_name: 'Ann', reason: 'ad_disabled',
      reason_text: 'their AD account is disabled', applied: false, seats_freed: 0,
    }],
  };
  const overview: AdStatusSyncOverview = {
    enabled: true, keycloak_configured: true, dry_run: true, nightly_hour_utc: 2,
    break_glass_usernames: ['breakglass@tn.local'], last_break_glass_use: null, latest: run, runs: [run],
  };

  beforeEach(async () => {
    directory = jasmine.createSpyObj('DirectoryApiService', ['adStatusSync', 'runAdStatusSync']);
    directory.adStatusSync.and.returnValue(of(overview));
    directory.runAdStatusSync.and.returnValue(of(run));
    await TestBed.configureTestingModule({
      imports: [AdStatusSyncPanelComponent, NoopAnimationsModule],
      providers: [{ provide: DirectoryApiService, useValue: directory }],
    }).compileComponents();
    fixture = TestBed.createComponent(AdStatusSyncPanelComponent);
    spyOn(fixture.debugElement.injector.get(MatSnackBar), 'open');
    fixture.detectChanges();
  });

  it('shows the last run, its counts and the accounts it found', () => {
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Dry run (records only, changes nothing)');
    expect(text).toContain('12 accounts looked at');
    expect(text).toContain('ann@corp.test');
    expect(text).toContain('No (would be)');
    expect(text).toContain('breakglass@tn.local');
  });

  it('"Run now" follows the server setting; "Dry run now" asks for a dry run', () => {
    const buttons = (fixture.nativeElement as HTMLElement).querySelectorAll('mat-card-actions button');
    (buttons[0] as HTMLButtonElement).click();
    expect(directory.runAdStatusSync).toHaveBeenCalledWith(true, false);
    (buttons[1] as HTMLButtonElement).click();
    expect(directory.runAdStatusSync).toHaveBeenCalledWith(undefined, false);
    expect(directory.adStatusSync).toHaveBeenCalledTimes(3); // reloaded after each run
  });

  it('says so when the panel cannot load', () => {
    directory.adStatusSync.and.returnValue(throwError(() => new Error('403')));
    fixture.componentInstance.load();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('could not be loaded');
  });
});
