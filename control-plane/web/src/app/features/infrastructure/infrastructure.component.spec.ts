import { TestBed, ComponentFixture } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { NEVER, of } from 'rxjs';
import { HypervisorConnection, HypervisorNode } from '@core/models';
import { InfrastructureApiService } from '@core/services/infrastructure-api.service';
import { InfrastructureComponent } from './infrastructure.component';

const VCENTER = {
  id: 'c1', name: 'Lab vCenter', hypervisor_type: 'vsphere', host: 'vcsa.lab.local', port: 443,
  username: 'svc-truenorth@vsphere.local', verify_ssl: false, is_primary: true, is_active: true,
  datacenter: 'DC-Lab', notes: null, tenant_id: null, created_at: '', updated_at: '',
} as HypervisorConnection;

describe('InfrastructureComponent (hypervisor connection form)', () => {
  let infra: jasmine.SpyObj<InfrastructureApiService>;
  let fixture: ComponentFixture<InfrastructureComponent>;
  let cmp: InfrastructureComponent;

  beforeEach(() => {
    infra = jasmine.createSpyObj('InfrastructureApiService', [
      'connections', 'connectionNodes', 'hypervisorSummary', 'createConnection', 'updateConnection',
      'appliances', 'storageSummary', 'networkDevices', 'networkSummary',
    ]);
    infra.connections.and.returnValue(of([]));
    infra.connectionNodes.and.returnValue(of([]));
    infra.hypervisorSummary.and.returnValue(NEVER);
    infra.appliances.and.returnValue(of([]));
    infra.storageSummary.and.returnValue(NEVER);
    infra.networkDevices.and.returnValue(of([]));
    infra.networkSummary.and.returnValue(NEVER);
    infra.createConnection.and.returnValue(of(VCENTER));
    infra.updateConnection.and.returnValue(of(VCENTER));

    TestBed.configureTestingModule({
      imports: [InfrastructureComponent, NoopAnimationsModule],
      providers: [{ provide: InfrastructureApiService, useValue: infra }],
    });
    fixture = TestBed.createComponent(InfrastructureComponent);
    cmp = fixture.componentInstance;
    fixture.detectChanges();
  });

  async function openTab(): Promise<HTMLElement> {
    // The form lives on the Compute tab; render it directly rather than clicking tabs.
    cmp.showAddConn = true;
    fixture.detectChanges();
    const tabs = fixture.nativeElement.querySelectorAll('[role="tab"]') as NodeListOf<HTMLElement>;
    tabs[1].click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows a Verify TLS toggle, on by default, with the VMCA hint, and a vSphere Datacenter field', async () => {
    const el = await openTab();
    const toggle = el.querySelector('[data-testid="conn-verify-ssl"]');
    expect(toggle).not.toBeNull();
    expect(toggle?.textContent).toContain('Verify TLS certificate');
    expect(el.textContent).toContain('Turn off for a vCenter using its own (VMCA) certificate');
    expect(toggle?.querySelector('button[role="switch"]')?.getAttribute('aria-checked')).toBe('true');
    expect(cmp.newConn.verify_ssl).toBeTrue();

    const dc = el.querySelector('[data-testid="conn-datacenter"]') as HTMLInputElement | null;
    expect(dc).not.toBeNull();
    expect(dc?.getAttribute('placeholder')).toBe('DC-Lab');
  });

  it('hides the Datacenter field for non-vSphere types', async () => {
    await openTab();
    cmp.newConn.hypervisor_type = 'proxmox';
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-testid="conn-datacenter"]')).toBeNull();
    expect(fixture.nativeElement.querySelector('[data-testid="conn-verify-ssl"]')).not.toBeNull();
  });

  it('turning the toggle off sends verify_ssl false and the datacenter on create', async () => {
    const el = await openTab();
    (el.querySelector('[data-testid="conn-verify-ssl"] button[role="switch"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(cmp.newConn.verify_ssl).toBeFalse();

    Object.assign(cmp.newConn, {
      name: 'Lab vCenter', host: 'vcsa.lab.local', username: 'svc', password: 'pw', datacenter: ' DC-Lab ',
    });
    cmp.createConnection();
    const body = infra.createConnection.calls.mostRecent().args[0];
    expect(body.verify_ssl).toBeFalse();
    expect(body.datacenter).toBe('DC-Lab');
    expect(body.hypervisor_type).toBe('vsphere');
  });

  it('does not send a datacenter for a non-vSphere connection', () => {
    Object.assign(cmp.newConn, { hypervisor_type: 'proxmox', datacenter: 'stale', password: 'pw' });
    cmp.createConnection();
    expect(infra.createConnection.calls.mostRecent().args[0].datacenter).toBeNull();
  });

  it('edit loads verify_ssl and datacenter, and update sends them without wiping the password', () => {
    cmp.startEditConnection(VCENTER);
    expect(cmp.newConn.verify_ssl).toBeFalse();
    expect(cmp.newConn.datacenter).toBe('DC-Lab');

    cmp.newConn.datacenter = 'DC-Range';
    cmp.updateConnection();
    const [id, body] = infra.updateConnection.calls.mostRecent().args;
    expect(id).toBe('c1');
    expect(body.verify_ssl).toBeFalse();
    expect(body.datacenter).toBe('DC-Range');
    expect('password' in body).toBeFalse();  // blank = keep; PATCH would 422 on ''
    expect('hypervisor_type' in body).toBeFalse();
  });

  it('update sends a retyped password', () => {
    cmp.startEditConnection(VCENTER);
    cmp.newConn.password = 'rotated';
    cmp.updateConnection();
    expect(infra.updateConnection.calls.mostRecent().args[1].password).toBe('rotated');
  });
});

describe('InfrastructureComponent (load gauges)', () => {
  const NODES = [
    { id: 'n1', connection_id: 'c1', node_name: 'esxi-01', status: 'online', ip_address: null, vm_count: 3,
      cpu_total: 16, cpu_used: 50, memory_total_gb: 100, memory_used_gb: 80, storage_total_gb: 1000, storage_used_gb: 950 },
    { id: 'n2', connection_id: 'c1', node_name: 'esxi-02', status: 'online', ip_address: null, vm_count: 0,
      cpu_total: null, cpu_used: null, memory_total_gb: null, memory_used_gb: null, storage_total_gb: null, storage_used_gb: null },
  ] as unknown as HypervisorNode[];

  function render(): ComponentFixture<InfrastructureComponent> {
    const infra = jasmine.createSpyObj('InfrastructureApiService', [
      'connections', 'connectionNodes', 'hypervisorSummary', 'appliances', 'storageSummary', 'networkDevices', 'networkSummary',
    ]);
    infra.connections.and.returnValue(of([VCENTER]));
    infra.connectionNodes.and.returnValue(of(NODES));
    infra.hypervisorSummary.and.returnValue(of({ total_connections: 1, total_nodes: 2, total_cpu: 16 }));
    infra.appliances.and.returnValue(of([]));
    infra.storageSummary.and.returnValue(NEVER);
    infra.networkDevices.and.returnValue(of([]));
    infra.networkSummary.and.returnValue(NEVER);
    TestBed.configureTestingModule({
      imports: [InfrastructureComponent, NoopAnimationsModule],
      providers: [{ provide: InfrastructureApiService, useValue: infra }],
    });
    const fixture = TestBed.createComponent(InfrastructureComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('shows a cluster load tile on Overview, aggregated over reporting hosts only', () => {
    const fixture = render();
    const card = fixture.nativeElement.querySelector('[data-testid="cluster-load"]') as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain('1 of 2 hosts reporting');
    const labels = Array.from(card.querySelectorAll('tn-load-gauge')).map(g => g.getAttribute('aria-label'));
    // esxi-02 reports nothing: it must not drag the cluster toward 0 %.
    expect(labels).toEqual(['CPU load 50 percent', 'Memory load 80 percent', 'Storage load 95 percent']);
  });

  it('Overview (the landing tab) shows the cluster tile first, then one host tile per host', () => {
    const fixture = render();
    const el = fixture.nativeElement as HTMLElement;
    const cluster = el.querySelector('[data-testid="cluster-load"]') as HTMLElement;
    const grid = el.querySelector('[data-testid="overview-host-load"]') as HTMLElement;
    expect(cluster).not.toBeNull();
    expect(grid).not.toBeNull();
    // Cluster tile precedes the host grid, and both precede the count cards.
    expect(cluster.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const firstStat = el.querySelector('.stat-card') as HTMLElement;
    expect(grid.compareDocumentPosition(firstStat) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const tiles = grid.querySelectorAll('tn-host-load-tile');
    expect(tiles.length).toBe(2);
    expect(tiles[0].textContent).toContain('esxi-01');
    expect(Array.from(tiles[0].querySelectorAll('tn-load-gauge')).map(g => g.getAttribute('aria-label')))
      .toEqual(['CPU load 50 percent', 'Memory load 80 percent', 'Storage load 95 percent']);
    const unreported = Array.from(tiles[1].querySelectorAll('tn-load-gauge'));
    expect(unreported.length).toBe(3);
    expect(unreported.every(g => g.classList.contains('band-unknown'))).toBeTrue();
    expect(tiles[1].textContent).toContain('not reported');
  });

  it('shows CPU/Memory/Storage gauges per host on Compute; unreported figures read "not reported"', async () => {
    const fixture = render();
    (fixture.nativeElement.querySelectorAll('[role="tab"]')[1] as HTMLElement).click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const tiles = fixture.nativeElement.querySelectorAll('[data-testid="host-load"] .host-tile') as NodeListOf<HTMLElement>;
    expect(tiles.length).toBe(2);
    const g1 = Array.from(tiles[0].querySelectorAll('tn-load-gauge'));
    expect(g1.map(g => g.classList[0])).toEqual(['band-normal', 'band-warning', 'band-critical']);
    const g2 = Array.from(tiles[1].querySelectorAll('tn-load-gauge'));
    expect(g2.every(g => g.classList.contains('band-unknown'))).toBeTrue();
    expect(tiles[1].textContent).toContain('not reported');
    expect(tiles[1].textContent).not.toContain('0%');
  });
});
