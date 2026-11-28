// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { CostReport, CostRow, RateCard, UsageApiService } from '@core/services/usage-api.service';
import { RateCardsComponent } from './rate-cards.component';
import { UsageCostComponent } from './usage-cost.component';

const FIG = {
  vcpu_hours: 0, ram_gb_hours: 0, disk_gb_hours: 0, vm_hours: 10, cost_vcpu: 0, cost_ram: 0, cost_disk: 0, cost_vm: 0,
  cost_total: 0, currency: 'CAD', unpriced_hours: 0,
};
const ROW = (key: string, label: string, total: number, over: Partial<CostRow> = {}): CostRow => ({
  ...FIG, key, label, tenant_id: 't1', intervals: 1, open: 0, approximate: false,
  cost_vcpu: total * 0.6, cost_ram: total * 0.2, cost_disk: total * 0.2, cost_total: total, ...over,
});
const REPORT: CostReport = {
  period_from: '2026-11-01T00:00:00Z', period_to: '2026-11-28T10:00:00Z', group_by: 'student',
  rows: [
    ROW('unattributed:t1', 'Unattributed (no Student)', 500),
    ROW('u-lee', 'Pte Morgan Lee', 30),
    ROW('u-fox', 'OCdt Jamie Fox', 90, { approximate: true }),
  ],
  totals: { ...FIG, cost_vcpu: 372, cost_ram: 124, cost_disk: 124, cost_total: 620 },
};

function auth(platform: boolean, perms: string[] = ['usage:read', 'usage:admin']) {
  return {
    isPlatformAdmin: signal(platform),
    me: signal({ status: 'registered', user: { id: 'me', role: 'admin', tenant_id: 't1' } }),
    can: (p: string) => perms.includes(p),
  };
}

describe('UsageCostComponent', () => {
  let fixture: ComponentFixture<UsageCostComponent>;
  let component: UsageCostComponent;
  let el: HTMLElement;
  let usage: jasmine.SpyObj<UsageApiService>;

  async function setup(platform = false) {
    usage = jasmine.createSpyObj('UsageApiService', ['cost', 'costCsv', 'student', 'rateCards', 'createRateCard']);
    usage.cost.and.returnValue(of(REPORT));
    usage.rateCards.and.returnValue(of([]));
    usage.student.and.returnValue(of({
      user_id: 'u-fox', period_from: REPORT.period_from, period_to: REPORT.period_to, totals: { ...FIG, cost_total: 90 },
      intervals: [{
        ...FIG, cost_total: 90, source: 'lab', source_id: 'l1', range_id: 'r1', course_id: 'c1', class_id: null,
        start: '2026-11-25T18:00:00Z', end: '2026-11-25T21:30:00Z', vcpu: 4, ram_gb: 8, disk_gb: 120, vms: 1,
        approximate: false, open: false, compute: true, range_name: 'lab-fox', course_name: 'Digital Forensics I', class_name: null,
      }],
    }));
    const core = jasmine.createSpyObj('ApiService', ['listTenants']);
    core.listTenants.and.returnValue(of([{ id: 't1', name: 'Kingston campus' }, { id: 't2', name: 'Borden campus' }]));
    await TestBed.configureTestingModule({
      imports: [UsageCostComponent],
      providers: [
        { provide: UsageApiService, useValue: usage },
        { provide: ApiService, useValue: core },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: auth(platform) },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(UsageCostComponent);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  it('shows the total, then named Students by cost, the unattributed line last', async () => {
    await setup();
    expect(usage.cost).toHaveBeenCalledWith(jasmine.objectContaining({ groupBy: 'student', tenantId: null }));
    const labels = [...el.querySelectorAll('tbody tr td:first-child')].map(td => td.textContent!.trim());
    expect(labels[0]).toContain('OCdt Jamie Fox');
    expect(labels[1]).toContain('Pte Morgan Lee');
    expect(labels[2]).toContain('Unattributed');
    expect(el.querySelector('.hero')!.textContent).toContain('$620.00');
    expect(el.querySelector('.hero')!.textContent).toContain('2 Students');
    expect(el.querySelector('.hero')!.textContent).toContain('OCdt Jamie Fox · $90.00');
    expect(el.textContent).toContain('≈ Approximate');
  });

  it('sorts by a column, and back', async () => {
    await setup();
    component.sortBy('total');
    fixture.detectChanges();
    expect(el.querySelector('tbody tr td')!.textContent).toContain('Pte Morgan Lee');
    expect(el.querySelector('tbody tr:last-child td')!.textContent).toContain('Unattributed');
  });

  it("opens a Student's drill-down, one group per range", async () => {
    await setup();
    (el.querySelector('tbody .rowlink') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(usage.student).toHaveBeenCalledWith('u-fox', jasmine.any(String), jasmine.any(String));
    const drill = el.querySelector('.drill')!.textContent!;
    expect(drill).toContain('Self-serve lab · lab-fox');
    expect(drill).toContain('Digital Forensics I');
    expect(drill).toContain('Running 3.5 h');
    // An unattributed line has no Student to drill into.
    expect(el.querySelectorAll('tbody .rowlink').length).toBe(2);
  });

  it('regroups by class and drops the Student-only figures', async () => {
    await setup();
    usage.cost.and.returnValue(of({ ...REPORT, group_by: 'class', rows: [ROW('k1', 'Cohort 3', 100)] }));
    component.setGroup('class');
    fixture.detectChanges();
    expect(usage.cost).toHaveBeenCalledWith(jasmine.objectContaining({ groupBy: 'class' }));
    expect(el.querySelector('.hero')!.textContent).not.toContain('Highest Student');
    expect(el.querySelectorAll('tbody .rowlink').length).toBe(0);
  });

  it('offers organisations only to the platform administrator', async () => {
    await setup(false);
    expect(el.querySelector('select[aria-label="Organisation"]')).toBeNull();
    expect([...el.querySelectorAll('.seg button')].map(b => b.textContent!.trim())).not.toContain('Organisation');
    TestBed.resetTestingModule();
    await setup(true);
    expect(el.querySelector('select[aria-label="Organisation"]')).not.toBeNull();
    component.tenantId.set('t2');
    component.load();
    expect(usage.cost).toHaveBeenCalledWith(jasmine.objectContaining({ tenantId: 't2' }));
  });

  it('says why when the report fails', async () => {
    await setup();
    usage.cost.and.returnValue(throwError(() => new HttpErrorResponse({ status: 403, error: { detail: 'Not permitted' } })));
    component.load();
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')).not.toBeNull();
  });
});

describe('RateCardsComponent', () => {
  let fixture: ComponentFixture<RateCardsComponent>;
  let component: RateCardsComponent;
  let el: HTMLElement;
  let usage: jasmine.SpyObj<UsageApiService>;
  const CARD = (id: string, tenant: string | null, from: string, disk = '0.000150'): RateCard => ({
    id, tenant_id: tenant, effective_from: from, currency: 'CAD', per_vcpu_hour: '0.018000', per_gb_ram_hour: '0.004500',
    per_gb_disk_hour: disk, per_vm_hour: '0.000000', notes: `card ${id}`, created_at: from,
  });

  async function setup(platform: boolean, perms?: string[]) {
    usage = jasmine.createSpyObj('UsageApiService', ['rateCards', 'createRateCard']);
    usage.rateCards.and.returnValue(of([
      CARD('old', null, '2026-10-01T00:00:00Z', '0.000120'), CARD('now', null, '2026-11-15T00:00:00Z', '0.000200'),
      CARD('next', null, '2099-01-01T00:00:00Z'), CARD('own', 't1', '2026-11-01T00:00:00Z'),
    ]));
    usage.createRateCard.and.returnValue(of(CARD('new', 't1', '2099-02-01T00:00:00Z')));
    await TestBed.configureTestingModule({
      imports: [RateCardsComponent],
      providers: [
        { provide: UsageApiService, useValue: usage },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: auth(platform, perms) },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RateCardsComponent);
    fixture.componentRef.setInput('tenants', [{ id: 't1', name: 'Kingston campus' }]);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  it('lists the platform default and the organisation, marking the card in force', async () => {
    await setup(false);
    const panels = [...el.querySelectorAll('section.panel:not(.hero)')];
    expect(panels[0].textContent).toContain('Platform default');
    expect(panels[1].textContent).toContain('Kingston campus');
    const inForce = [...el.querySelectorAll('.chip.ok')].map(c => c.closest('.row')!.textContent);
    expect(inForce.length).toBe(2);
    expect(inForce[0]).toContain('card now');
    expect(el.querySelector('.chip.neutral')!.closest('.row')!.textContent).toContain('card next');
    // Disk shows per GB-month: 0.000200 x 730.
    expect(panels[0].textContent).toContain('0.146/GB-month');
  });

  it('adds a card for the organisation; only the platform administrator may pick the default', async () => {
    await setup(false);
    (el.querySelector('.hero .primary') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el.querySelector('select')).toBeNull();
    expect(el.textContent).toContain('Only the platform administrator sets the platform default');
    const f = component.form()!;
    expect(f.platformWide).toBeFalse();
    expect(f.vcpu).toBe('0.018'); // prefilled from the card in force
    component.form.set({ ...f, effectiveFrom: '2099-02-01T00:00' });
    component.save();
    expect(usage.createRateCard).toHaveBeenCalledWith(jasmine.objectContaining({ platform_wide: false, per_vcpu_hour: '0.018000' }));
    expect(component.form()).toBeNull();
  });

  it('shows the form error instead of sending a card in the past', async () => {
    await setup(true);
    component.open();
    component.form.set({ ...component.form()!, effectiveFrom: '2020-01-01T00:00' });
    component.save();
    fixture.detectChanges();
    expect(usage.createRateCard).not.toHaveBeenCalled();
    expect(el.querySelector('[role="alert"]')!.textContent).toContain('never changes');
  });

  it('fills the rates from the helper', async () => {
    await setup(true);
    component.open();
    for (const [k, v] of [['hardware', 1800000], ['monthly', 9000], ['vcpu', 3000], ['ramGb', 24000], ['diskTb', 600]] as const) {
      component.setHelper(k, v);
    }
    component.useSuggested();
    expect(component.form()!.vcpu).toBe(component.suggested()!.vcpu.toFixed(6));
  });

  it('hides New rate card without usage:admin', async () => {
    await setup(false, ['usage:read']);
    expect(el.querySelector('.hero .primary')).toBeNull();
  });
});

describe('UsageCostComponent periods', () => {
  it('asks the drill-down and the CSV for the same period as the table', async () => {
    const usage = jasmine.createSpyObj('UsageApiService', ['cost', 'costCsv', 'student']);
    usage.cost.and.returnValue(of(REPORT));
    usage.costCsv.and.returnValue(of(new Blob(['x'])));
    usage.student.and.returnValue(of({ user_id: 'u-fox', period_from: '', period_to: '', totals: FIG, intervals: [] }));
    const core = jasmine.createSpyObj('ApiService', ['listTenants']);
    core.listTenants.and.returnValue(of([]));
    await TestBed.configureTestingModule({
      imports: [UsageCostComponent],
      providers: [
        { provide: UsageApiService, useValue: usage },
        { provide: ApiService, useValue: core },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: AuthService, useValue: auth(false) },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(UsageCostComponent);
    fixture.detectChanges();
    const asked = usage.cost.calls.mostRecent().args[0];
    await new Promise(r => setTimeout(r, 5)); // "now" moves on
    fixture.componentInstance.select(REPORT.rows[2]);
    spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
    spyOn(HTMLAnchorElement.prototype, 'click');
    fixture.componentInstance.downloadCsv();
    expect(usage.student).toHaveBeenCalledWith('u-fox', asked.from, asked.to);
    expect(usage.costCsv).toHaveBeenCalledWith(jasmine.objectContaining({ from: asked.from, to: asked.to }));
  });
});
