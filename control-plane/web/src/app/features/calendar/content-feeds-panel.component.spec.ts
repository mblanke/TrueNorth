import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ContentReviewApiService, FeedStatus } from '@core/services/content-review-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ContentFeedsPanelComponent } from './content-feeds-panel.component';

const ONLINE: FeedStatus = {
  mode: 'online', can_import: false, entries: 214, flagged_items: 3, sources: [
    { source: 'endoflife', label: 'endoflife.date (releases and end of life)', last_success_at: '2026-10-27T04:10:00Z',
      last_attempt_at: '2026-10-27T04:10:00Z', last_error: '', entries: 40 },
    { source: 'nvd', label: 'NVD CVE API 2.0', last_attempt_at: '2026-10-27T04:12:00Z', last_success_at: null,
      last_error: 'services.nvd.nist.gov answered HTTP 503', entries: 0 },
  ],
};
const BUNDLE: FeedStatus = {
  mode: 'bundle', can_import: true, bundle_key_id: 'ab12cd34ef56ab12', entries: 0, flagged_items: 0, sources: [
    { source: 'bundle', label: 'Signed feed bundle', last_error: '', entries: 0 },
  ],
};

describe('ContentFeedsPanelComponent', () => {
  let fixture: ComponentFixture<ContentFeedsPanelComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ContentReviewApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  function make(status: FeedStatus): void {
    api.feedStatus.and.returnValue(of(status));
    fixture = TestBed.createComponent(ContentFeedsPanelComponent);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  beforeEach(async () => {
    api = jasmine.createSpyObj('ContentReviewApiService', ['feedStatus', 'importFeedBundle']);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);
    await TestBed.configureTestingModule({
      imports: [ContentFeedsPanelComponent, NoopAnimationsModule],
      providers: [
        { provide: ContentReviewApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    }).compileComponents();
  });

  const importButton = () =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Import bundle')) as
      HTMLButtonElement | undefined;

  it('shows each source with its last pull and its error, and no import online', () => {
    make(ONLINE);
    const sources = Array.from(el.querySelectorAll('.source'));
    expect(el.querySelector('.mode')?.textContent).toBe('Online');
    expect(sources.length).toBe(2);
    expect(sources[0].textContent).toContain('endoflife.date');
    expect(sources[0].textContent).toContain('Last pull');
    expect(sources[1].querySelector('.err')?.textContent).toContain('HTTP 503');
    expect(sources[1].textContent).toContain('Never');
    expect(el.textContent).toContain('214 entries · 3 items flagged');
    expect(importButton()).toBeUndefined();
  });

  it('says feeds are off', () => {
    make({ mode: 'off', can_import: false, sources: [], entries: 0, flagged_items: 0 });
    expect(el.textContent).toContain('feeds are off');
    expect(el.querySelector('code')?.textContent).toBe('tn_feeds_mode');
  });

  it('imports a bundle and reloads the status', () => {
    make(BUNDLE);
    api.importFeedBundle.and.returnValue(of({ entries: 5, new: 5, updated: 0, generated_at: '2026-10-27T04:00:00Z',
      key_id: 'ab12cd34ef56ab12' }));
    const input = el.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['{}'], 'feeds.json', { type: 'application/json' });
    Object.defineProperty(input, 'files', { value: [file] });
    fixture.componentInstance.picked(input);
    expect(api.importFeedBundle).toHaveBeenCalledWith(file);
    expect(notify.success).toHaveBeenCalled();
    expect(api.feedStatus).toHaveBeenCalledTimes(2);
  });

  it('shows a refused bundle in words', () => {
    make(BUNDLE);
    api.importFeedBundle.and.returnValue(throwError(() => new HttpErrorResponse({ status: 422,
      error: { detail: "The bundle's signature does not verify against this site's feed key; nothing was imported" } })));
    const input = el.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File(['{}'], 'feeds.json')] });
    fixture.componentInstance.picked(input);
    expect(notify.error).toHaveBeenCalledWith(jasmine.stringContaining('does not verify'));
  });

  it('disables import and says why when it cannot be done', () => {
    make({ ...BUNDLE, can_import: false, bundle_key_problem: 'No feed bundle public key is configured' });
    expect(importButton()?.disabled).toBeTrue();
    expect(el.textContent).toContain('No feed bundle public key is configured');
  });
});
