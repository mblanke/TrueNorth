import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { environment } from '@env/environment';
import { RangeRecoveryApiService } from './range-recovery-api.service';
import { OperatorRecordsApiService } from './operator-records-api.service';

describe('R0-B operator API clients (URLs and bodies match docs/interfaces/openapi.json)', () => {
  let ranges: RangeRecoveryApiService;
  let records: OperatorRecordsApiService;
  let http: HttpTestingController;
  const base = environment.apiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [HttpClientTestingModule] });
    ranges = TestBed.inject(RangeRecoveryApiService);
    records = TestBed.inject(OperatorRecordsApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('range operations, lease and reservations', () => {
    ranges.listOperations('r1').subscribe();
    http.expectOne(`${base}/ranges/r1/operations`).flush([]);
    ranges.getOperation('r1', 'o1').subscribe();
    http.expectOne(`${base}/ranges/r1/operations/o1`).flush({});
    ranges.abandonOperation('r1', 'o1').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/operations/o1/abandon`).request.method).toBe('POST');
    ranges.getLease('r1').subscribe();
    http.expectOne(`${base}/ranges/r1/lease`).flush({});
    ranges.listNetworkReservations('r1').subscribe();
    http.expectOne(`${base}/ranges/r1/network-reservations`).flush([]);
  });

  it('force-release repeats the range id; abandon range sends the reason', () => {
    ranges.forceReleaseLease('r1', 'vCenter checked').subscribe();
    const fr = http.expectOne(`${base}/ranges/r1/lease/force-release`);
    expect(fr.request.body).toEqual({ confirm_range_id: 'r1', reason: 'vCenter checked' });
    fr.flush({});
    ranges.abandonRange('r1', 'VMs gone by hand').subscribe();
    const ab = http.expectOne(`${base}/ranges/r1/abandon`);
    expect(ab.request.body).toEqual({ reason: 'VMs gone by hand' });
    ab.flush({});
  });

  it('snapshots', () => {
    ranges.createSnapshot('r1', 'base', '').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/snapshots`).request.body).toEqual({ name: 'base', description: null });
    ranges.restoreSnapshot('r1', 's1').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/snapshots/s1/restore`).request.method).toBe('POST');
    ranges.deleteSnapshot('r1', 's1').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/snapshots/s1`).request.method).toBe('DELETE');
  });

  it('team membership passes user and role as query parameters', () => {
    records.addTeamMember('t1', 'u1', 'lead').subscribe();
    const req = http.expectOne(r => r.url === `${base}/teams/t1/members`);
    expect(req.request.method).toBe('POST');
    expect(req.request.params.get('user_id')).toBe('u1');
    expect(req.request.params.get('role')).toBe('lead');
    req.flush({});
    records.removeTeamMember('t1', 'u1').subscribe();
    expect(http.expectOne(`${base}/teams/t1/members/u1`).request.method).toBe('DELETE');
  });

  it('course progress, completion and Moodle results', () => {
    records.getProgress('c1', 'u1').subscribe();
    http.expectOne(`${base}/courses/c1/progress/u1`).flush([]);
    records.markComplete('c1', 'u1').subscribe();
    expect(http.expectOne(`${base}/courses/c1/complete/u1`).request.method).toBe('POST');
    records.getMoodleResults('p1').subscribe();
    http.expectOne(`${base}/integrations/platforms/p1/moodle-results`).flush({});
    records.pullMoodleResults('p1', true).subscribe();
    expect(http.expectOne(`${base}/integrations/platforms/p1/moodle-results/pull`).request.body).toEqual({ reset: true });
  });
});
