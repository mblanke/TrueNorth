import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, flushMicrotasks, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { NEVER, of, throwError } from 'rxjs';
import { HypervisorConnection, HypervisorNode, InfraDiscovery } from '@core/models';
import { InfrastructureApiService } from '@core/services/infrastructure-api.service';
import { InfrastructureComponent } from './infrastructure.component';
import { REREAD_MS, cadence, overallUpdated, updatedAgo } from './freshness';

const conn = (id: string, name: string, is_active = true) => ({
  id, name, hypervisor_type: 'vsphere', host: `${name}.lab.local`, port: 443,
  username: 'svc-truenorth@vsphere.local', verify_ssl: true, is_primary: false, is_active,
  datacenter: null, notes: null, tenant_id: null, created_at: '', updated_at: '',
} as HypervisorConnection);

const node = (memUsed: number) => ({
  id: 'n1', connection_id: 'c1', node_name: 'esxi-01', status: 'online', ip_address: null, vm_count: 3,
  cpu_total: 16, cpu_used: 50, memory_total_gb: 100, memory_used_gb: memUsed, storage_total_gb: 1000, storage_used_gb: 500,
} as unknown as HypervisorNode);

const minsAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

describe('InfrastructureComponent (re-reading the stored inventory)', () => {
  let infra: jasmine.SpyObj<InfrastructureApiService>;
  let fixture: ComponentFixture<InfrastructureComponent>;
  let visibility: DocumentVisibilityState;

  function setVisibility(state: DocumentVisibilityState): void {
    visibility = state;
    document.dispatchEvent(new Event('visibilitychange'));
  }

  function reads(): number {
    return infra.discoveryStatus.calls.count();
  }

  beforeEach(() => {
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    infra = jasmine.createSpyObj('InfrastructureApiService', [
      'connections', 'connectionNodes', 'hypervisorSummary', 'appliances', 'storageSummary', 'networkDevices',
      'networkSummary', 'discoveryStatus', 'discoverConnection',
    ]);
    infra.connections.and.returnValue(of([conn('c1', 'vc-a')]));
    infra.connectionNodes.and.returnValue(of([node(40)]));
    infra.hypervisorSummary.and.returnValue(NEVER);
    infra.discoveryStatus.and.returnValue(of({ interval_seconds: 300, connections: [] } as InfraDiscovery));
    infra.appliances.and.returnValue(of([]));
    infra.storageSummary.and.returnValue(NEVER);
    infra.networkDevices.and.returnValue(of([]));
    infra.networkSummary.and.returnValue(NEVER);
    TestBed.configureTestingModule({
      imports: [InfrastructureComponent, NoopAnimationsModule],
      providers: [{ provide: InfrastructureApiService, useValue: infra }],
    });
  });

  afterEach(() => {
    delete (document as unknown as Record<string, unknown>)['visibilityState'];
  });

  function create(): void {
    fixture = TestBed.createComponent(InfrastructureComponent);
    fixture.detectChanges();
  }

  it('re-reads every minute while visible, and the Overview gauges move with it', fakeAsync(() => {
    create();
    expect(reads()).toBe(1);
    const memGauge = () => (fixture.nativeElement.querySelector('[data-testid="cluster-load"] tn-load-gauge:nth-child(2)') as HTMLElement)
      .getAttribute('aria-label');
    expect(memGauge()).toBe('Memory load 40 percent');

    infra.connectionNodes.and.returnValue(of([node(90)]));  // the scheduled discovery stored new figures
    tick(REREAD_MS - 1);
    expect(reads()).toBe(1);
    tick(1);
    fixture.detectChanges();
    expect(reads()).toBe(2);
    expect(infra.connections).toHaveBeenCalledTimes(2);
    expect(infra.hypervisorSummary).toHaveBeenCalledTimes(2);
    expect(memGauge()).toBe('Memory load 90 percent');

    fixture.destroy();
    discardPeriodicTasks();
  }));

  it('makes no requests while the tab is hidden, and re-reads at once when it is shown again', fakeAsync(() => {
    create();
    setVisibility('hidden');
    tick(REREAD_MS * 5);
    expect(reads()).toBe(1);
    expect(infra.connections).toHaveBeenCalledTimes(1);

    setVisibility('visible');
    flushMicrotasks();
    expect(reads()).toBe(2);
    tick(REREAD_MS);
    expect(reads()).toBe(3);

    fixture.destroy();
    discardPeriodicTasks();
  }));

  it('stops re-reading when the page is left', fakeAsync(() => {
    create();
    fixture.destroy();
    tick(REREAD_MS * 3);
    setVisibility('visible');
    expect(reads()).toBe(1);
  }));

  it('shows "Updated" per connection and overall (the stalest active one), and failing ones', fakeAsync(() => {
    infra.connections.and.returnValue(of([conn('c1', 'vc-a'), conn('c2', 'vc-b'), conn('c3', 'vc-old', false)]));
    infra.discoveryStatus.and.returnValue(of({
      interval_seconds: 300,
      connections: [
        { connection_id: 'c1', last_discovered_at: minsAgo(2), failing: false, running: false },
        { connection_id: 'c2', last_discovered_at: minsAgo(7), failing: true, last_error: 'timed out', running: false },
        { connection_id: 'c3', last_discovered_at: minsAgo(600), failing: false, running: false },
      ],
    } as InfraDiscovery));
    create();
    const el = fixture.nativeElement as HTMLElement;
    const bar = el.querySelector('[data-testid="infra-freshness"]') as HTMLElement;
    expect(bar.textContent).toContain('Updated 7 min ago');  // c2; the inactive c3 does not count
    expect(el.querySelector('[data-testid="infra-freshness-failing"]')?.textContent).toContain('1 connection failing');

    (el.querySelectorAll('[role="tab"]')[1] as HTMLElement).click();
    fixture.detectChanges();
    tick();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="conn-updated-c1"]')?.textContent).toContain('2 min ago');
    expect(el.querySelector('[data-testid="conn-updated-c2"]')?.textContent).toContain('7 min ago');

    fixture.destroy();
    discardPeriodicTasks();
  }));

  it('Refresh now rediscovers every connection, counts one already running as such, then re-reads', fakeAsync(() => {
    infra.connections.and.returnValue(of([conn('c1', 'vc-a'), conn('c2', 'vc-b')]));
    infra.discoverConnection.and.callFake((id: string) =>
      id === 'c1' ? of({ message: 'Discovered 0 new hosts (2 total)' }) : throwError(() => ({ status: 409 })));
    create();
    const before = reads();
    (fixture.nativeElement.querySelector('[data-testid="infra-refresh-now"]') as HTMLButtonElement).click();
    flushMicrotasks();
    tick(10_000);  // the snack bar's own timer
    expect(infra.discoverConnection.calls.allArgs()).toEqual([['c1'], ['c2']]);
    expect(reads()).toBe(before + 1);
    expect(fixture.componentInstance.discoveringAll).toBeFalse();

    fixture.destroy();
    discardPeriodicTasks();
  }));
});

describe('freshness helpers', () => {
  const now = new Date('2026-10-20T12:00:00Z');

  it('says how long ago, and "never" for nothing', () => {
    expect(updatedAgo(null, now)).toBe('never');
    expect(updatedAgo('2026-10-20T11:59:40Z', now)).toBe('just now');
    expect(updatedAgo('2026-10-20T11:55:00+00:00', now)).toBe('5 min ago');
    expect(updatedAgo('2026-10-20T09:50:00Z', now)).toBe('2 h 10 min ago');
    expect(updatedAgo('2026-10-17T12:00:00Z', now)).toBe('3 d ago');
  });

  it('states the cadence, or nothing when the schedule is off', () => {
    expect(cadence(300)).toBe('every 5 min');
    expect(cadence(90)).toBe('every 90 s');
    expect(cadence(0)).toBe('');
  });

  it('overall is null while an active connection has never been discovered', () => {
    const rows = [{ connection_id: 'c1', last_discovered_at: '2026-10-20T11:55:00Z' }];
    expect(overallUpdated([conn('c1', 'a'), conn('c2', 'b')], rows)).toBeNull();
    expect(overallUpdated([conn('c1', 'a'), conn('c2', 'b', false)], rows)).toBe('2026-10-20T11:55:00Z');
    expect(overallUpdated([], rows)).toBeNull();
  });
});
