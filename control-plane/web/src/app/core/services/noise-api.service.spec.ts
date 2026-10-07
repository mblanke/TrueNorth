import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { ApiService } from './api.service';
import { NoiseApiService } from './noise-api.service';

describe('NoiseApiService', () => {
  let svc: NoiseApiService;
  let api: jasmine.SpyObj<ApiService>;

  beforeEach(() => {
    api = jasmine.createSpyObj('ApiService', ['get', 'put', 'post']);
    api.get.and.returnValue(of([]));
    api.put.and.returnValue(of({}));
    api.post.and.returnValue(of({}));
    TestBed.configureTestingModule({ providers: [{ provide: ApiService, useValue: api }] });
    svc = TestBed.inject(NoiseApiService);
  });

  it('goes through the shared client at the contract paths', () => {
    svc.profile('r 1').subscribe();
    svc.agents('r1').subscribe();
    svc.personas('r1').subscribe();
    svc.stats('r1', 30).subscribe();
    svc.presets().subscribe();
    expect(api.get.calls.allArgs().map(a => a[0])).toEqual([
      '/noise/ranges/r%201',
      '/noise/ranges/r1/agents',
      '/noise/ranges/r1/personas',
      '/noise/ranges/r1/stats?minutes=30',
      '/noise/presets',
    ]);
  });

  it('sends only the changed fields, and a dry run as such', () => {
    svc.update('r1', { enabled: false }).subscribe();
    svc.deploy('r1', true).subscribe();
    expect(api.put).toHaveBeenCalledWith('/noise/ranges/r1', { enabled: false });
    expect(api.post).toHaveBeenCalledWith('/noise/ranges/r1/deploy', { dry_run: true, refresh_targets: true });
  });

  it('builds the activity and plan queries', () => {
    svc.activity('r1').subscribe();
    svc.activity('r1', { limit: 5, lookalike: true }).subscribe();
    svc.plan('r1', 'lnx 01', 30).subscribe();
    expect(api.get.calls.allArgs().map(a => a[0])).toEqual([
      '/noise/ranges/r1/activity?limit=100',
      '/noise/ranges/r1/activity?limit=5&lookalike=true',
      '/noise/ranges/r1/plan?node=lnx%2001&minutes=30',
    ]);
  });
});
