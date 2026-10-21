import { TestBed, ComponentFixture } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { HypervisorConnection, VmInventory } from '@core/models';
import { InfrastructureApiService } from '@core/services/infrastructure-api.service';
import { VmInventoryComponent } from './vm-inventory.component';

const conn = (id: string, primary = false) => ({
  id, name: `vc-${id}`, hypervisor_type: 'vsphere', host: `${id}.lab`, port: 443, username: 'svc',
  verify_ssl: true, is_primary: primary, is_active: true, datacenter: null, notes: null, tenant_id: null,
  created_at: '', updated_at: '',
}) as HypervisorConnection;

const INVENTORY: VmInventory = {
  supported: true,
  message: '5 VMs',
  groups: { golden: 1, copies: 1, ranges: 1, other: 2 },
  vms: [
    { vm_id: 'vm-1', name: 'ubuntu-lts', power_state: 'POWERED_OFF', host: null, group: 'golden',
      tags: { 'tn-type': 'golden-template', 'tn-os': 'ubuntu-2404', 'tn-template': 'ubuntu-lts' } },
    { vm_id: 'vm-2', name: 'ubuntu-lts@esx02-20261020', power_state: 'POWERED_OFF', host: null, group: 'copies',
      tags: { 'tn-type': 'template-copy', 'tn-template': 'ubuntu-lts' } },
    { vm_id: 'vm-10', name: '2fc8d04d-ubu01', power_state: 'POWERED_ON', host: 'esx02.truenorth.lab', group: 'ranges',
      tags: { 'tn-type': 'range-vm', 'tn-range': 'ubuntu-lab-2fc8d04d' } },
    { vm_id: 'vm-11', name: 'WIN11-TMPL', power_state: 'POWERED_OFF', host: 'esx02.truenorth.lab', group: 'other', tags: {} },
    { vm_id: 'vm-12', name: 'TN-DEPOT01', power_state: 'POWERED_ON', host: 'esx03.truenorth.lab', group: 'other',
      tags: { 'tn-type': 'depot' } },
  ],
};

describe('VmInventoryComponent (VMs grouped by tn-type)', () => {
  let infra: jasmine.SpyObj<InfrastructureApiService>;
  let fixture: ComponentFixture<VmInventoryComponent>;
  let cmp: VmInventoryComponent;

  function setConnections(cs: HypervisorConnection[]): void {
    fixture.componentRef.setInput('connections', cs);
    fixture.detectChanges();
  }

  const el = () => fixture.nativeElement as HTMLElement;
  const rows = () => Array.from(el().querySelectorAll('tr.mat-mdc-row')).map(r => r.querySelector('td')?.textContent?.trim());
  const chips = () => Array.from(el().querySelectorAll('mat-chip-option')) as HTMLElement[];
  /** What a click on the chip lands on: its option action, not the host element. */
  const pick = (i: number) => (chips()[i].querySelector('[role="option"]') as HTMLElement).click();

  beforeEach(() => {
    infra = jasmine.createSpyObj('InfrastructureApiService', ['connectionVms']);
    infra.connectionVms.and.returnValue(of(INVENTORY));
    TestBed.configureTestingModule({
      imports: [VmInventoryComponent, NoopAnimationsModule],
      providers: [{ provide: InfrastructureApiService, useValue: infra }],
    });
    fixture = TestBed.createComponent(VmInventoryComponent);
    cmp = fixture.componentInstance;
  });

  it('does not reach vCenter until asked, and picks the primary connection', () => {
    setConnections([conn('a'), conn('b', true)]);
    expect(infra.connectionVms).not.toHaveBeenCalled();
    expect(el().textContent).toContain('Load the connection');
    (el().querySelector('[data-testid="vm-load"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(infra.connectionVms).toHaveBeenCalledOnceWith('b');
  });

  it('shows every VM with group counts, then filters by group', async () => {
    setConnections([conn('a')]);
    cmp.load();
    fixture.detectChanges();
    expect(chips().map(c => c.textContent?.trim())).toEqual([
      'All (5)', 'Golden templates (1)', 'Host copies (1)', 'Range VMs (1)', 'Other (2)']);
    expect(rows().length).toBe(5);

    pick(3);  // Range VMs
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(rows()).toEqual(['2fc8d04d-ubu01']);
    const cells = Array.from(el().querySelectorAll('tr.mat-mdc-row td')).map(td => td.textContent?.trim());
    expect(cells).toEqual(['2fc8d04d-ubu01', 'range-vm', '—', '—', 'ubuntu-lab-2fc8d04d', 'esx02.truenorth.lab', 'On']);

    pick(4);  // Other: untagged and non-template TrueNorth VMs
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(rows()).toEqual(['WIN11-TMPL', 'TN-DEPOT01']);
  });

  it('says so when the hypervisor cannot list VMs', () => {
    infra.connectionVms.and.returnValue(of({ supported: false, message: 'Listing VMs is not supported for proxmox connections', vms: [] }));
    setConnections([conn('a')]);
    cmp.load();
    fixture.detectChanges();
    expect(el().textContent).toContain('Listing VMs is not supported for proxmox connections');
    expect(chips().length).toBe(0);
  });

  it('reports a failed load instead of an empty list', () => {
    infra.connectionVms.and.returnValue(throwError(() => ({ error: { detail: 'Connection not found' } })));
    setConnections([conn('a')]);
    cmp.load();
    fixture.detectChanges();
    expect(el().querySelector('[data-testid="vm-note"]')?.textContent).toContain('Could not load VMs: Connection not found');
  });

  it('asks for a connection when there is none', () => {
    setConnections([]);
    expect(el().textContent).toContain('No hypervisor connection');
    expect(el().querySelector('[data-testid="vm-load"]')).toBeNull();
  });
});
