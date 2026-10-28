import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { fakeAuth } from '@core/auth/fake-auth';
import {
  ContentItem, ContentItemDetail, ContentReviewApiService, ContentTheme,
} from '@core/services/content-review-api.service';
import { NotificationService } from '@core/services/notification.service';
import { ContentPortApiService, ProvenStatus } from '@core/services/content-port-api.service';
import { ContentCalendarComponent } from './content-calendar.component';

const NOT_PROVEN = {
  item_type: 'course', item_id: 'c', title: 'C201 Network Defence', proven: false,
  label: 'Not proven: No class of it has been closed with a passing Student yet',
  reasons: ['No class of it has been closed with a passing Student yet'], conditions: [], dependencies: [],
  golden_builds: [],
} as unknown as ProvenStatus;

const BLUE: ContentTheme = { id: 'blue', name: 'Blue team', colour: '#3B5BDB', ordinal: 0, platform_wide: true };
const PLATFORM: ContentTheme = { id: 'plat', name: 'Platform', colour: '#6B7280', ordinal: 7, platform_wide: true };

function at(daysFromToday: number): string {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() + daysFromToday);
  return d.toISOString();
}

function item(over: Partial<ContentItem>): ContentItem {
  return {
    id: 'i', item_type: 'course', item_id: 'c', title: 'Item', shared: false, theme_ids: [], cycle_days: 90,
    due_at: at(0), clock: { state: 'green', days: 30 }, stale: false, stale_reasons: [], retired: false,
    can_review: true, can_assign: true, can_admin: true, ...over,
  } as ContentItem;
}

const GOLDEN = item({
  id: 'g', item_type: 'golden_template', title: 'ubuntu-2404', shared: true, theme_ids: ['plat'], stale: true,
  clock: { state: 'stale', days: 30 }, can_review: false, can_assign: false, can_admin: false,
  stale_reasons: [
    { kind: 'release', text: 'New Ubuntu 24.04 point release', origin_id: 'g', source: 'release:ubuntu:24.04:24.04.6' },
    { kind: 'vulnerability', text: 'CVE-2026-21001 in CISA KEV (Ubuntu 24.04)', origin_id: 'g',
      source: 'kev:CVE-2026-21001' },
    { kind: 'time', text: 'Review overdue by 3 days', origin_id: 'g', source: 'clock' },
  ],
});
const COURSE = item({ id: 'c', title: 'C201 Network Defence', theme_ids: ['blue'], due_at: at(0),
  clock: { state: 'amber', days: 9 } });

function detail(it: ContentItem, over: Partial<ContentItemDetail> = {}): ContentItemDetail {
  return { ...it, depends_on: [], used_by: [], ...over } as ContentItemDetail;
}

describe('ContentCalendarComponent', () => {
  let fixture: ComponentFixture<ContentCalendarComponent>;
  let component: ContentCalendarComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ContentReviewApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let auth: ReturnType<typeof fakeAuth>;
  let port: jasmine.SpyObj<ContentPortApiService>;

  beforeEach(async () => {
    api = jasmine.createSpyObj('ContentReviewApiService',
      ['list', 'stale', 'get', 'markReviewed', 'assignReviewer', 'retire', 'themes', 'feedStatus', 'importFeedBundle']);
    api.feedStatus.and.returnValue(of({ mode: 'online', can_import: false, sources: [], entries: 12,
      flagged_items: 1 }));
    api.themes.and.returnValue(of([BLUE, PLATFORM]));
    api.list.and.returnValue(of({ items: [GOLDEN, COURSE], theme_counts: { blue: 1, plat: 1 }, total: 2 }));
    api.stale.and.returnValue(of([GOLDEN]));
    api.get.and.callFake((id: string) => of(id === 'g'
      ? detail(GOLDEN, { used_by: [{ id: 't', item_type: 'range_template', title: 'Medium Enterprise', stale: true,
        retired: false }] })
      : detail(COURSE)));
    api.markReviewed.and.returnValue(of(detail({ ...COURSE, clock: { state: 'green', days: 90 } })));
    api.retire.and.returnValue(of(detail({ ...COURSE, retired: true })));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    auth = fakeAuth('instructor');
    port = jasmine.createSpyObj('ContentPortApiService', ['proven', 'items', 'signingKey', 'exportBundle', 'preview',
      'importBundle']);
    port.proven.and.returnValue(of(NOT_PROVEN));
    const core = jasmine.createSpyObj('ApiService', ['listUsers']);
    core.listUsers.and.returnValue(of([
      { id: 'u1', display_name: 'Ada', role: 'instructor' }, { id: 'u2', display_name: 'Sam', role: 'student' },
    ]));

    await TestBed.configureTestingModule({
      imports: [ContentCalendarComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ContentReviewApiService, useValue: api },
        { provide: ApiService, useValue: core },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: auth },
        { provide: ContentPortApiService, useValue: port },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ContentCalendarComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
  });

  const button = (text: string) =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.trim().startsWith(text)) as HTMLButtonElement;

  it('has the Events | Content switch, Events linking to the Events view', () => {
    const events = el.querySelector('nav.segmented a') as HTMLAnchorElement;
    expect(events.textContent).toContain('Events');
    expect(events.getAttribute('href')).toBe('/calendar/events');
    expect(el.querySelector('nav.segmented [aria-current="page"]')?.textContent).toContain('Content');
  });

  it('puts items on their due dates, stale ones outlined with a clock', () => {
    const items = Array.from(el.querySelectorAll('button.item'));
    expect(items.length).toBe(2);
    const stale = items.find(b => b.textContent?.includes('ubuntu-2404'))!;
    expect(stale.classList).toContain('stale');
    expect(stale.querySelector('mat-icon.clock')).not.toBeNull();
    expect(stale.getAttribute('aria-label')).toBe('Stale: ubuntu-2404');
  });

  it('shows theme chips with counts, and a chip filters', () => {
    const chips = Array.from(el.querySelectorAll('button.chip'));
    expect(chips.map(c => c.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['Blue team1', 'Platform1']);
    (chips[1] as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(chips[1].getAttribute('aria-pressed')).toBe('false');
    expect(el.querySelectorAll('button.item').length).toBe(1);
    expect(el.querySelectorAll('button.stale-row').length).toBe(0);
  });

  it('lists what is stale now and opens the first one', () => {
    expect(el.querySelector('section[aria-label="Stale now"] .badge')?.textContent).toBe('1');
    expect(el.querySelector('.stale-row .reason')?.textContent).toContain('point release');
    expect(api.get).toHaveBeenCalledWith('g');
    const panel = el.querySelector('section[aria-label="Selected content"]')!;
    expect(panel.textContent).toContain('Platform');
    expect(panel.textContent).toContain('Golden template');
    expect(panel.querySelector('.clock-badge')?.classList).toContain('stale');
    expect(panel.textContent).toContain('Every 90 days');
    expect(panel.textContent).toContain('Medium Enterprise');
  });

  it('lists every stale reason with what raised it, a feed by name', () => {
    const reasons = Array.from(el.querySelectorAll('section[aria-label="Selected content"] .reasons li'));
    expect(reasons.length).toBe(3);
    expect(reasons[0].textContent).toContain('New Ubuntu 24.04 point release');
    expect(reasons[0].querySelector('.feed')?.textContent).toBe('endoflife.date');
    expect(reasons[1].textContent).toContain('CVE-2026-21001 in CISA KEV');
    expect(reasons[1].querySelector('.feed')?.textContent).toBe('CISA KEV');
    expect(reasons[1].querySelector('mat-icon')?.textContent).toBe('gpp_maybe');
    expect(reasons[2].querySelector('.feed')).toBeNull();
  });

  it('shows the Feeds panel to admins only', () => {
    expect(el.querySelector('tn-content-feeds-panel')).toBeNull();
    expect(api.feedStatus).not.toHaveBeenCalled();
    auth.role.set('admin');
    fixture.detectChanges();
    expect(el.querySelector('tn-content-feeds-panel')).not.toBeNull();
    expect(api.feedStatus).toHaveBeenCalled();
    expect(el.querySelector('section[aria-label="Feeds"]')?.textContent).toContain('12 entries');
  });

  it('disables actions the API would refuse', () => {
    expect(button('Mark reviewed').disabled).toBeTrue();
    expect(button('Assign reviewer').disabled).toBeTrue();
    expect(button('Retire').disabled).toBeTrue();
  });

  it('marks reviewed and reloads', () => {
    component.select('c');
    fixture.detectChanges();
    expect(el.querySelector('.clock-badge')?.classList).toContain('amber');
    button('Mark reviewed').click();
    fixture.detectChanges();
    expect(api.markReviewed).toHaveBeenCalledWith('c');
    expect(notify.success).toHaveBeenCalled();
    expect(el.querySelector('.clock-badge')?.classList).toContain('green');
    expect(api.list.calls.count()).toBeGreaterThan(1);
  });

  it('offers only admins and instructors as reviewers', () => {
    component.select('c');
    fixture.detectChanges();
    button('Assign reviewer').click();
    fixture.detectChanges();
    expect(component.staff().map(u => u.display_name)).toEqual(['Ada']);
  });

  it('retires only with a reason, and shows a refusal in words', () => {
    component.select('c');
    fixture.detectChanges();
    button('Retire').click();
    fixture.detectChanges();
    expect(button('Retire now').disabled).toBeTrue();
    api.retire.and.returnValue(throwError(() => new HttpErrorResponse({ status: 403,
      error: { detail: 'Only an admin can do this' } })));
    component.retireReason = 'replaced';
    component.retire();
    fixture.detectChanges();
    expect(component.actionError()).toBe('Only an admin can do this');
  });

  it('switches between week and month', () => {
    expect(el.querySelectorAll('.cell').length).toBe(42);
    component.setView('week');
    fixture.detectChanges();
    expect(el.querySelectorAll('.cell').length).toBe(7);
  });

  it('shows the selected course its Proven chip, and none for a golden template', () => {
    component.select('c');
    fixture.detectChanges();
    expect(port.proven).toHaveBeenCalledWith('course', 'c');
    expect(el.querySelector('tn-proven-chip .chip')?.textContent).toContain('Not proven: No class of it');
    port.proven.calls.reset();
    component.select('g');
    fixture.detectChanges();
    expect(port.proven).not.toHaveBeenCalled();
    expect(el.querySelector('tn-proven-chip .chip')).toBeNull();
  });

  it('shows the Port content panel to the platform administrator only', () => {
    expect(el.querySelector('tn-content-port-panel')).toBeNull();
    auth.role.set('admin');
    fixture.detectChanges();
    expect(el.querySelector('tn-content-port-panel')).toBeNull();  // a tenant's admin
    auth.platformAdmin.set(true);
    fixture.detectChanges();
    expect(el.querySelector('tn-content-port-panel')).not.toBeNull();
  });
});
