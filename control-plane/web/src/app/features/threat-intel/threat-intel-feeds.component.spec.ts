import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { ThreatIntelApiService } from '@core/services/threat-intel-api.service';
import { ThreatIntelFeedsComponent } from './threat-intel-feeds.component';
import { routes } from '../../app.routes';

describe('ThreatIntelFeedsComponent', () => {
  let fixture: ComponentFixture<ThreatIntelFeedsComponent>;
  let component: ThreatIntelFeedsComponent;
  let api: jasmine.SpyObj<ThreatIntelApiService>;
  let snack: jasmine.SpyObj<MatSnackBar>;

  const feed = {
    id: 'f1', name: 'northwind', feed_type: 'csv', url: 'https://feeds.example.org/iocs.csv',
    poll_interval_minutes: 60, is_enabled: true, last_poll_at: null, last_poll_status: null,
    indicator_count: 0, tenant_id: 't1', created_at: '2026-10-07T00:00:00Z', updated_at: '2026-10-07T00:00:00Z',
  };
  const uploadOnly = { ...feed, id: 'f2', name: 'manual', url: null };
  const pulled = {
    feed: { ...feed, indicator_count: 2, last_poll_status: 'partial', last_poll_at: '2026-10-07T10:00:00Z' },
    status: 'partial', created: 2, updated: 0, deactivated: 0, rejected: 1,
    rejections: [{ row: 3, reason: 'value is not a valid ipv4 address' }],
  };

  function text(): string {
    return (fixture.nativeElement as HTMLElement).textContent || '';
  }

  beforeEach(async () => {
    api = jasmine.createSpyObj('ThreatIntelApiService', ['feeds', 'createFeed', 'pull', 'upload']);
    api.feeds.and.returnValue(of([feed, uploadOnly]));
    snack = jasmine.createSpyObj('MatSnackBar', ['open']);
    await TestBed.configureTestingModule({
      imports: [ThreatIntelFeedsComponent, NoopAnimationsModule],
      providers: [{ provide: ThreatIntelApiService, useValue: api }],
    })
      .overrideProvider(MatSnackBar, { useValue: snack })
      .compileComponents();
    fixture = TestBed.createComponent(ThreatIntelFeedsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('is reachable under Authoring as the Threat intel tab', () => {
    const authoring = routes.find(r => r.path === 'authoring')!;
    expect((authoring.data!['tabs'] as { path: string }[]).map(t => t.path)).toContain('threat-intel');
    expect(authoring.children!.some(c => c.path === 'threat-intel' && !!c.loadComponent)).toBeTrue();
  });

  it('lists feeds; "Pull now" only for a feed with a URL, upload for every feed', () => {
    expect(api.feeds).toHaveBeenCalled();
    expect(text()).toContain('northwind');
    expect(text()).toContain('never');
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('button[aria-label^="Pull "]').length).toBe(1);
    expect(el.querySelectorAll('button[aria-label^="Upload a CSV"]').length).toBe(2);
  });

  it('pulls now and shows the counts and rejected rows', () => {
    api.pull.and.returnValue(of(pulled));

    component.pull(feed);
    fixture.detectChanges();

    expect(api.pull).toHaveBeenCalledWith('f1');
    expect(text()).toContain('2 created');
    expect(text()).toContain('Row 3: value is not a valid ipv4 address');
    expect(component.feeds()[0].indicator_count).toBe(2);
    expect(component.busy()).toBeNull();
  });

  it('uploads the chosen file and clears the input so it can be chosen again', () => {
    api.upload.and.returnValue(of({ ...pulled, feed: { ...uploadOnly, indicator_count: 2 } }));
    const file = new File(['type,value\n'], 'iocs.csv', { type: 'text/csv' });
    const input = { files: [file], value: 'C:\\fakepath\\iocs.csv' } as unknown as HTMLInputElement;

    component.onFile(uploadOnly, input);

    expect(api.upload).toHaveBeenCalledWith('f2', file);
    expect(input.value).toBe('');
    expect(component.feeds().find(f => f.id === 'f2')!.indicator_count).toBe(2);
  });

  it('says why a pull failed and reloads the feed list', () => {
    api.pull.and.returnValue(throwError(() => ({ status: 502, error: { detail: 'the feed answered HTTP 503' } })));
    api.feeds.calls.reset();

    component.pull(feed);

    expect(snack.open).toHaveBeenCalledWith('the feed answered HTTP 503', 'Close', jasmine.any(Object));
    expect(api.feeds).toHaveBeenCalled();
    expect(component.lastPull()).toBeNull();
    expect(component.busy()).toBeNull();
  });

  it('adds a CSV feed, with no URL when left empty', () => {
    api.createFeed.and.returnValue(of({ ...uploadOnly, id: 'f3', name: 'added' }));
    component.newName = ' added ';
    component.newUrl = '  ';

    component.addFeed();

    expect(api.createFeed).toHaveBeenCalledWith({ name: 'added', feed_type: 'csv', url: null });
    expect(component.feeds().map(f => f.name)).toContain('added');
    expect(component.newName).toBe('');
  });
});
