import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { environment } from '@env/environment';
import { GreyspaceApiService } from './greyspace-api.service';

describe('GreyspaceApiService', () => {
  let api: GreyspaceApiService;
  let http: HttpTestingController;
  const base = environment.apiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    api = TestBed.inject(GreyspaceApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists corpus tiers', () => {
    api.corpora().subscribe();
    expect(http.expectOne(`${base}/greyspace/corpora`).request.method).toBe('GET');
  });

  it('reads a range status and config', () => {
    api.status('r1').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/greyspace`).request.method).toBe('GET');
    api.config('r1').subscribe();
    expect(http.expectOne(`${base}/ranges/r1/greyspace/config`).request.method).toBe('GET');
  });

  it('attaches with PUT and the block as the body', () => {
    api.attach('r1', { site_packs: ['news'] }).subscribe();
    const req = http.expectOne(`${base}/ranges/r1/greyspace`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ site_packs: ['news'] });
    req.flush({});
  });

  it('detaches with DELETE', () => {
    api.detach('r 1').subscribe();
    expect(http.expectOne(`${base}/ranges/r%201/greyspace`).request.method).toBe('DELETE');
  });
});
