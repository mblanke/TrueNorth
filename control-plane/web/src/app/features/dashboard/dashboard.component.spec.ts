import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { RouterTestingModule } from '@angular/router/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { DashboardComponent, usagePct } from './dashboard.component';
import { ApiService } from '@core/services/api.service';
import { Range, Exercise, HealthResponse, HypervisorNode } from '@core/models';

/** An ESXi host as vSphere discovery stores it: VM count, but no CPU/memory figures. */
const esxiHost: HypervisorNode = {
  id: 'n1', connection_id: 'c1', node_name: 'esxi01.range.test', ip_address: null,
  status: 'online', cpu_total: null, cpu_used: null, memory_total_gb: null, memory_used_gb: null,
  storage_total_gb: null, storage_used_gb: null, vm_count: 12, last_seen_at: '2026-09-24T12:00:00Z',
};

describe('DashboardComponent', () => {
  let component: DashboardComponent;
  let fixture: ComponentFixture<DashboardComponent>;
  let mockApi: jasmine.SpyObj<ApiService>;

  const mockHealth: HealthResponse = {
    status: 'ok',
    version: '0.1.0',
    app: 'truenorth',
    db: true,
    redis: true,
  };

  const mockRanges: Partial<Range>[] = [
    { id: 'r1', name: 'Range A', state: 'ready' },
    { id: 'r2', name: 'Range B', state: 'provisioning' },
    { id: 'r3', name: 'Range C', state: 'destroyed' },
  ];

  const mockExercises: Partial<Exercise>[] = [
    { id: 'e1', name: 'Ex 1', state: 'running', total_score: 75, max_score: 100 },
    { id: 'e2', name: 'Ex 2', state: 'completed', total_score: 90, max_score: 100 },
  ];

  beforeEach(async () => {
    mockApi = jasmine.createSpyObj('ApiService', [
      'health',
      'listRanges',
      'listExercises',
      'hypervisorNodes',
      'getCapacity',
      'listScheduledEvents',
      'checkCapacity',
      'createScheduledEvent',
      'deleteScheduledEvent',
    ]);
    mockApi.health.and.returnValue(of(mockHealth));
    mockApi.listRanges.and.returnValue(of(mockRanges as Range[]));
    mockApi.listExercises.and.returnValue(of(mockExercises as Exercise[]));
    mockApi.hypervisorNodes.and.returnValue(of([]));
    mockApi.getCapacity.and.returnValue(of({}));
    mockApi.listScheduledEvents.and.returnValue(of([]));
    mockApi.checkCapacity.and.returnValue(of({}));
    mockApi.createScheduledEvent.and.returnValue(of({}));
    mockApi.deleteScheduledEvent.and.returnValue(of(void 0));

    await TestBed.configureTestingModule({
      imports: [
        DashboardComponent,
        NoopAnimationsModule,
        RouterTestingModule,
      ],
      providers: [
        { provide: ApiService, useValue: mockApi },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(DashboardComponent);
    component = fixture.componentInstance;
  });

  // ── Creation ─────────────────────────────────────────────────────
  it('should create', () => {
    expect(component).toBeTruthy();
  });

  // ── Loads range stats on init ────────────────────────────────────
  it('should load range stats on init', () => {
    fixture.detectChanges();  // triggers ngOnInit

    expect(mockApi.health).toHaveBeenCalled();
    expect(mockApi.listRanges).toHaveBeenCalledWith(5);
    expect(mockApi.listExercises).toHaveBeenCalledWith(5);
  });

  // ── Displays range count ─────────────────────────────────────────
  it('should display active range count (provisioning + ready + running)', () => {
    fixture.detectChanges();

    // 'ready' and 'provisioning' are active, 'destroyed' is not
    expect(component.rangeCount()).toBe(2);
  });

  // ── Displays exercise count ──────────────────────────────────────
  it('should display exercise count', () => {
    fixture.detectChanges();

    expect(component.exerciseCount()).toBe(2);
  });

  // ── Handles loading state ────────────────────────────────────────
  it('should show initial empty state before data loads', () => {
    // Before ngOnInit
    expect(component.ranges()).toEqual([]);
    expect(component.exercises()).toEqual([]);
    expect(component.health()).toBeNull();
    expect(component.rangeCount()).toBe(0);
    expect(component.exerciseCount()).toBe(0);
  });

  // ── Handles error state ──────────────────────────────────────────
  it('should handle API errors gracefully', () => {
    mockApi.listRanges.and.returnValue(throwError(() => new Error('Network error')));
    mockApi.listExercises.and.returnValue(throwError(() => new Error('Network error')));
    mockApi.health.and.returnValue(throwError(() => new Error('Network error')));

    // Should not throw
    expect(() => fixture.detectChanges()).not.toThrow();

    // State remains at defaults
    expect(component.ranges()).toEqual([]);
    expect(component.exerciseCount()).toBe(0);
  });

  // ── Health display ─────────────────────────────────────────────
  it('should set health data from API', () => {
    fixture.detectChanges();

    expect(component.health()?.status).toBe('ok');
    expect(component.health()?.version).toBe('0.1.0');
  });

  // ── healthTooltip ──────────────────────────────────────────────
  it('healthTooltip() should format DB and Redis status', () => {
    fixture.detectChanges();

    const tip = component.healthTooltip();
    expect(tip).toContain('DB: connected');
    expect(tip).toContain('Redis: connected');
  });

  it('healthTooltip() should return empty string when health is null', () => {
    expect(component.healthTooltip()).toBe('');
  });

  // ── Rendered output ────────────────────────────────────────────
  it('should render stat cards in the template', () => {
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const statValues = el.querySelectorAll('.stat-value');
    expect(statValues.length).toBeGreaterThanOrEqual(2);
  });

  // ── Cluster panel: vSphere host inventory ──────────────────────
  it('reads the stored host inventory, not a live Proxmox cluster', () => {
    mockApi.hypervisorNodes.and.returnValue(of([esxiHost]));
    fixture.detectChanges();
    expect(mockApi.hypervisorNodes).toHaveBeenCalled();
    expect(component.clusterNodes().length).toBe(1);
  });

  it('shows an ESXi host with its VM count, and CPU/RAM as not reported rather than 0%', () => {
    mockApi.hypervisorNodes.and.returnValue(of([esxiHost]));
    fixture.detectChanges();
    const card: HTMLElement = fixture.nativeElement.querySelector('.node-card');
    expect(card.textContent).toContain('esxi01.range.test');
    expect(card.textContent).toContain('12 VMs');
    expect(card.querySelectorAll('.not-reported').length).toBe(2);
    expect(card.querySelector('.tn-gauge-fill')).toBeNull();
    expect(card.textContent).not.toContain('0%');
  });

  it('points to Infrastructure when no hosts have been discovered', () => {
    fixture.detectChanges();
    const empty: HTMLElement = fixture.nativeElement.querySelector('.empty-card');
    expect(empty.textContent).toContain('vCenter');
    expect(empty.querySelector('a[href="/infrastructure"]')).not.toBeNull();
  });

  it('usagePct() is null when a figure is not reported', () => {
    expect(usagePct(null, 256)).toBeNull();
    expect(usagePct(64, null)).toBeNull();
    expect(usagePct(64, 0)).toBeNull();
    expect(usagePct(64, 256)).toBe(25);
  });
});