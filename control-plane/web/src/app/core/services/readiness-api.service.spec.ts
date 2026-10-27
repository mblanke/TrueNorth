import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { environment } from '@env/environment';
import { ReadinessApiService } from './readiness-api.service';

describe('ReadinessApiService', () => {
  let api: ReadinessApiService;
  let http: HttpTestingController;
  const base = `${environment.apiUrl}/readiness`;

  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [HttpClientTestingModule] });
    api = TestBed.inject(ReadinessApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('reads the page with only the filters that are set', () => {
    api.get({ dateFrom: '2026-01-01', dateTo: '', unit: '  ' }).subscribe();
    const req = http.expectOne(r => r.url === base);
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('date_from')).toBe('2026-01-01');
    expect(req.request.params.has('date_to')).toBeFalse();
    expect(req.request.params.has('unit')).toBeFalse();
    req.flush({});
  });

  it('exports CSV as text and PDF as a blob, with the same filters', () => {
    api.exportCsv({ dateFrom: '', dateTo: '2026-12-31', unit: '2 Sig Regt' }).subscribe();
    const csv = http.expectOne(r => r.url === `${base}/export.csv`);
    expect(csv.request.responseType).toBe('text');
    expect(csv.request.params.get('unit')).toBe('2 Sig Regt');
    expect(csv.request.params.get('date_to')).toBe('2026-12-31');
    csv.flush('class\n');
    api.exportPdf({ dateFrom: '', dateTo: '', unit: '' }).subscribe();
    const pdf = http.expectOne(r => r.url === `${base}/export.pdf`);
    expect(pdf.request.responseType).toBe('blob');
    pdf.flush(new Blob(['%PDF']));
  });
});
