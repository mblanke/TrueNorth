import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { environment } from '@env/environment';
import { CourseStudioApiService } from './course-studio-api.service';

describe('CourseStudioApiService', () => {
  let api: CourseStudioApiService;
  let http: HttpTestingController;
  const base = environment.apiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    api = TestBed.inject(CourseStudioApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists releases, optionally for one course', () => {
    api.releases().subscribe();
    http.expectOne(`${base}/course-releases`).flush([]);
    api.releases('c 1').subscribe();
    http.expectOne(`${base}/course-releases?course_id=c%201`).flush([]);
  });

  it('accepts with the acknowledged actions', () => {
    api.accept('r1', { acknowledge_actions: ['po.x'], notes: 'ok' }).subscribe();
    const req = http.expectOne(`${base}/course-releases/r1/accept`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ acknowledge_actions: ['po.x'], notes: 'ok' });
    req.flush({});
  });

  it('publishes to one platform and retries a publication', () => {
    api.publish('r1', 'p1').subscribe();
    expect(http.expectOne(`${base}/course-releases/r1/publications`).request.body).toEqual({ platform_id: 'p1' });
    api.retryPublication('pub1').subscribe();
    expect(http.expectOne(`${base}/course-publications/pub1/retry`).request.method).toBe('POST');
  });

  it('uses the lab token route and header when a token is given', () => {
    api.lab('s1', 'tok').subscribe();
    const byToken = http.expectOne(`${base}/lab-access/s1`);
    expect(byToken.request.headers.get('X-Lab-Token')).toBe('tok');
    byToken.flush({});
    api.labAction('s1', 'reset', 'tok').subscribe();
    expect(http.expectOne(`${base}/lab-access/s1/reset`).request.headers.get('X-Lab-Token')).toBe('tok');
  });

  it('uses the signed-in lab routes without a token', () => {
    api.lab('s1').subscribe();
    const signedIn = http.expectOne(`${base}/lab-sessions/s1`);
    expect(signedIn.request.headers.has('X-Lab-Token')).toBeFalse();
    signedIn.flush({});
    api.labConsole('s1').subscribe();
    http.expectOne(`${base}/lab-sessions/s1/console`).flush({});
  });
});
