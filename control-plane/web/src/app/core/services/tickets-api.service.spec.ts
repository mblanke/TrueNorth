import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { environment } from '@env/environment';
import { TicketsApiService } from './tickets-api.service';
import { WikiApiService } from './wiki-api.service';

describe('TicketsApiService / WikiApiService URLs', () => {
  let tickets: TicketsApiService;
  let wiki: WikiApiService;
  let http: HttpTestingController;
  const base = environment.apiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [HttpClientTestingModule] });
    tickets = TestBed.inject(TicketsApiService);
    wiki = TestBed.inject(WikiApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists tickets with only the filters that are set', () => {
    tickets.list({ scope: 'mine', status: 'open,waiting', q: '' }).subscribe();
    const req = http.expectOne(r => r.url === `${base}/tickets`);
    expect(req.request.params.get('scope')).toBe('mine');
    expect(req.request.params.get('status')).toBe('open,waiting');
    expect(req.request.params.has('q')).toBeFalse();
    req.flush([]);
  });

  it('moves a card on the board', () => {
    tickets.move('t1', 'in_progress', 1.5).subscribe();
    const req = http.expectOne(`${base}/tickets/t1/move`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ status: 'in_progress', board_order: 1.5 });
    req.flush({});
  });

  it('sends internal notes with the flag', () => {
    tickets.addComment('t1', 'note', true).subscribe();
    const req = http.expectOne(`${base}/tickets/t1/comments`);
    expect(req.request.body).toEqual({ body: 'note', is_internal: true });
    req.flush({});
  });

  it('downloads attachments as a blob through HttpClient (so auth goes with it)', () => {
    tickets.download('a1').subscribe();
    const req = http.expectOne(`${base}/tickets/attachments/a1`);
    expect(req.request.responseType).toBe('blob');
    req.flush(new Blob(['x']));
  });

  it('uploads attachments as multipart form data', () => {
    tickets.upload('t1', [new File(['x'], 'log.txt')]).subscribe();
    const req = http.expectOne(`${base}/tickets/t1/attachments`);
    expect(req.request.body instanceof FormData).toBeTrue();
    req.flush([]);
  });

  it('saves wiki pages with the base revision', () => {
    wiki.updatePage('p1', { base_revision: 3, body: 'x' }).subscribe();
    const req = http.expectOne(`${base}/wiki/pages/p1`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.base_revision).toBe(3);
    req.flush({});
  });

  it('creates pages inside a space and encodes the slug', () => {
    wiki.createPage('run books', { title: 'A' }).subscribe();
    http.expectOne(`${base}/wiki/spaces/run%20books/pages`).flush({});
  });

  it('restores a revision', () => {
    wiki.restore('p1', 2, 5).subscribe();
    const req = http.expectOne(`${base}/wiki/pages/p1/revisions/2/restore`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ base_revision: 5 });
    req.flush({});
  });
});
