import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { HTTP_INTERCEPTORS, HttpRequest, HttpHandler, HttpInterceptor, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { ApiService } from './api.service';
import { environment } from '@env/environment';
import { Range } from '@core/models';

// ── Fake auth interceptor to prove header injection ──────────────────
let fakeToken: string | null = null;

@Injectable()
class FakeAuthInterceptor implements HttpInterceptor {
  intercept(req: HttpRequest<unknown>, next: HttpHandler) {
    if (fakeToken) {
      req = req.clone({ setHeaders: { Authorization: `Bearer ${fakeToken}` } });
    }
    return next.handle(req);
  }
}

describe('ApiService', () => {
  let service: ApiService;
  let httpMock: HttpTestingController;
  const base = environment.apiUrl;

  beforeEach(() => {
    fakeToken = null;

    TestBed.configureTestingModule({
    imports: [],
    providers: [
        ApiService,
        { provide: HTTP_INTERCEPTORS, useClass: FakeAuthInterceptor, multi: true },
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
    ]
});

    service = TestBed.inject(ApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();          // no outstanding requests
  });

  // ── GET /api/templates/windows-roles ───────────────────────────────
  it('getWindowsRoles() reads the role catalogue the designer grid is built from', () => {
    const specs = { vcpu: 2, ram_mb: 4096, disk_gb: 60 };
    service.getWindowsRoles().subscribe((cat) => {
      expect(cat.groups).toEqual(['Identity']);
      expect(cat.roles[0].id).toBe('ad-ds');
    });
    const req = httpMock.expectOne(`${base}/templates/windows-roles`);
    expect(req.request.method).toBe('GET');
    req.flush({
      groups: ['Identity'], base: specs,
      roles: [{ id: 'ad-ds', label: 'Active Directory', group: 'Identity', min: specs, recommended: specs,
                method: 'feature', images: [], requires: [], conflicts: [], aliases: ['active_directory'], notes: '' }],
    });
  });

  // ── GET /api/ranges ────────────────────────────────────────────────
  it('getRanges() should make GET /api/ranges', () => {
    const mockRanges: Partial<Range>[] = [
      { id: 'r1', name: 'Range A', state: 'ready' },
      { id: 'r2', name: 'Range B', state: 'created' },
    ];

    service.listRanges().subscribe((ranges) => {
      expect(ranges.length).toBe(2);
      expect(ranges[0].name).toBe('Range A');
    });

    const req = httpMock.expectOne(
      (r) => r.url === `${base}/ranges` && r.method === 'GET',
    );
    expect(req.request.params.get('limit')).toBe('50');
    expect(req.request.params.get('offset')).toBe('0');
    req.flush(mockRanges);
  });

  // ── POST /api/ranges ───────────────────────────────────────────────
  it('createRange() should make POST /api/ranges with body', () => {
    const body = { name: 'New Range', template_id: 'tpl-1' };
    const mockResp: Partial<Range> = { id: 'r3', name: 'New Range', state: 'created' };

    service.createRange(body).subscribe((range) => {
      expect(range.id).toBe('r3');
      expect(range.state).toBe('created');
    });

    const req = httpMock.expectOne(`${base}/ranges`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(body);
    req.flush(mockResp);
  });

  // ── POST /api/ranges/{id}/provision ────────────────────────────────
  it('provisionRange() should make POST /api/ranges/{id}/provision', () => {
    const mockResp: Partial<Range> = { id: 'r1', state: 'provisioning' };

    service.provisionRange('r1').subscribe((range) => {
      expect(range.state).toBe('provisioning');
    });

    const req = httpMock.expectOne(`${base}/ranges/r1/provision`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush(mockResp);
  });

  // ── Error handling ─────────────────────────────────────────────────
  it('should return observable error on HTTP failure', () => {
    service.listRanges().subscribe({
      next: () => fail('expected error'),
      error: (err) => {
        expect(err.status).toBe(500);
      },
    });

    const req = httpMock.expectOne((r) => r.url === `${base}/ranges`);
    req.flush('Internal Server Error', { status: 500, statusText: 'Server Error' });
  });

  // ── Auth header injection ──────────────────────────────────────────
  it('should include Authorization header when token is present', () => {
    fakeToken = 'test-jwt-token';

    service.health().subscribe();

    const req = httpMock.expectOne(`${base}/health`);
    expect(req.request.headers.get('Authorization')).toBe('Bearer test-jwt-token');
    req.flush({ status: 'ok', version: '0.1.0', app: 'truenorth', db: true, redis: true });
  });

  // ── Base URL is configurable ───────────────────────────────────────
  it('should use the configured base URL from environment', () => {
    service.health().subscribe();

    const req = httpMock.expectOne(`${base}/health`);
    expect(req.request.url).toContain(environment.apiUrl);
    req.flush({ status: 'ok', version: '0.1.0', app: 'truenorth', db: true, redis: true });
  });

  // ── Exercises ──────────────────────────────────────────────────────
  it('listExercises() should make GET /api/exercises', () => {
    service.listExercises().subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/exercises` && r.method === 'GET');
    req.flush([]);
  });

  it('startExercise() should POST to /api/exercises/{id}/start', () => {
    service.startExercise('ex-1').subscribe();
    const req = httpMock.expectOne(`${base}/exercises/ex-1/start`);
    expect(req.request.method).toBe('POST');
    req.flush({ id: 'ex-1', state: 'running' });
  });

  // ── Destroy range ──────────────────────────────────────────────────
  it('destroyRange() should POST to /api/ranges/{id}/destroy', () => {
    service.destroyRange('r1').subscribe();
    const req = httpMock.expectOne(`${base}/ranges/r1/destroy`);
    expect(req.request.method).toBe('POST');
    req.flush({ id: 'r1', state: 'destroying' });
  });

  // ── Designer topology ──────────────────────────────────────────────
  it('saveRangeTopology() should POST the diagram to /ranges/{id}/topology', () => {
    const diagram = { cells: [{ id: 'n1' }] };
    service.saveRangeTopology('r1', diagram).subscribe(res => expect(res.node_count).toBe(1));
    const req = httpMock.expectOne(`${base}/ranges/r1/topology`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ diagram_json: diagram });
    req.flush({ range_id: 'r1', template_id: 't1', created: true, node_count: 1, vlan_count: 1, warnings: [], template: {} });
  });

  it('templateFromDiagram() should POST the diagram and name to /templates/from-diagram', () => {
    const diagram = { cells: [] };
    service.templateFromDiagram(diagram, 'Lab').subscribe();
    const req = httpMock.expectOne(`${base}/templates/from-diagram`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ diagram_json: diagram, name: 'Lab' });
    req.flush({ template: {}, yaml: '', warnings: [] });
  });

  // ── Templates ──────────────────────────────────────────────────────
  it('listTemplates() should make GET /api/templates with params', () => {
    service.listTemplates(10, 5).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/templates`);
    expect(req.request.params.get('limit')).toBe('10');
    expect(req.request.params.get('offset')).toBe('5');
    req.flush([]);
  });

  // ── AAR / Telemetry ────────────────────────────────────────────────
  it('generateAAR() should POST', () => {
    service.generateAAR('ex-1').subscribe();
    const req = httpMock.expectOne(`${base}/exercises/ex-1/aar/generate`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('searchTelemetry() should GET with query params', () => {
    service.searchTelemetry('r1', 'process:cmd.exe', 10).subscribe();
    const req = httpMock.expectOne((r) => r.url === `${base}/telemetry/r1/search`);
    expect(req.request.params.get('q')).toBe('process:cmd.exe');
    expect(req.request.params.get('size')).toBe('10');
    req.flush({ hits: { hits: [] } });
  });

  // ── Detections (ADR 0005) ──────────────────────────────────────────
  it('submitDetection() POSTs the query to the objective detections endpoint', () => {
    service.submitDetection('ex1', 'OBJ-1', 'process.name:cmd.exe').subscribe(d => {
      expect(d.verdict).toBe('missed');
      expect(d.attempts_left).toBe(4);
    });
    const req = httpMock.expectOne(`${base}/exercises/ex1/objectives/OBJ-1/detections`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ query: 'process.name:cmd.exe' });
    req.flush({
      id: 'd1', objective_ref: 'OBJ-1', user_id: 'u1', query: 'process.name:cmd.exe',
      submitted_at: '2026-10-06T10:00:00Z', verdict: 'missed', events_matched: 3, attempts_left: 4,
    });
  });

  it('submitDetection() encodes the objective ref', () => {
    service.submitDetection('ex1', 'a/b', 'x').subscribe();
    const req = httpMock.expectOne(`${base}/exercises/ex1/objectives/a%2Fb/detections`);
    expect(req.request.method).toBe('POST');
    req.flush({});
  });

  it('listDetections() GETs the exercise attempts', () => {
    service.listDetections('ex1').subscribe(rows => expect(rows.length).toBe(1));
    const req = httpMock.expectOne(`${base}/exercises/ex1/detections`);
    expect(req.request.method).toBe('GET');
    req.flush([{ id: 'd1', objective_ref: 'OBJ-1', user_id: 'u1', query: 'q', submitted_at: null, verdict: 'missed', events_matched: 0 }]);
  });
});
