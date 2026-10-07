import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { environment } from '@env/environment';
import { ThreatIntelApiService } from './threat-intel-api.service';

describe('ThreatIntelApiService', () => {
  let api: ThreatIntelApiService;
  let http: HttpTestingController;
  const base = `${environment.apiUrl}/threat-intel`;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    api = TestBed.inject(ThreatIntelApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists, creates and deletes feeds', () => {
    api.feeds().subscribe();
    http.expectOne(`${base}/feeds`).flush([]);
    api.createFeed({ name: 'n', feed_type: 'csv', url: null }).subscribe();
    const create = http.expectOne(`${base}/feeds`);
    expect(create.request.method).toBe('POST');
    expect(create.request.body).toEqual({ name: 'n', feed_type: 'csv', url: null });
    create.flush({});
    api.deleteFeed('f1').subscribe();
    expect(http.expectOne(`${base}/feeds/f1`).request.method).toBe('DELETE');
  });

  it('pulls a feed now', () => {
    api.pull('f1').subscribe();
    const req = http.expectOne(`${base}/feeds/f1/pull`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('uploads the feed file as multipart field "file"', () => {
    const file = new File(['type,value\nipv4,203.0.113.7\n'], 'iocs.csv', { type: 'text/csv' });
    api.upload('f1', file).subscribe();
    const req = http.expectOne(`${base}/feeds/f1/upload`);
    expect(req.request.method).toBe('POST');
    expect((req.request.body as FormData).get('file')).toEqual(jasmine.any(File));
    req.flush({});
  });

  it('reads a feed\'s indicators', () => {
    api.indicators('f1', 20).subscribe();
    http.expectOne(`${base}/feeds/f1/indicators?limit=20`).flush([]);
  });
});
