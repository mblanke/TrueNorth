import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { RangeSummary, TelemetryEvent } from '@core/models';
import {
  TelemetryComponent, rangeMetricsSummary, telemetrySearchError, telemetryTechniques,
} from './telemetry.component';

describe('telemetryTechniques', () => {
  it('joins the list the API tags events with', () => {
    expect(telemetryTechniques({ mitre_technique: ['T1071', 'T1573'] })).toBe('T1071, T1573');
  });

  it('passes a single string through', () => {
    expect(telemetryTechniques({ mitre_technique: 'T1059' })).toBe('T1059');
  });

  it('is empty for an untagged event', () => {
    expect(telemetryTechniques({ event_type: 'range_metrics' })).toBe('');
  });
});

describe('rangeMetricsSummary', () => {
  const base: TelemetryEvent = { event_type: 'range_metrics', source: 'vsphere', vm_count: 3, vms_powered_on: 2 };

  it('shows the measured figures and where they came from', () => {
    expect(rangeMetricsSummary({ ...base, cpu_pct: 12.5, memory_pct: 50 }))
      .toBe('CPU 12.5% · Memory 50% · 2/3 VMs on · vsphere');
  });

  it('says "Not reported" for a figure the hypervisor did not give, never 0%', () => {
    const line = rangeMetricsSummary({ ...base, memory_pct: 25 });
    expect(line).toContain('CPU Not reported');
    expect(line).not.toContain('CPU 0%');
  });

  it('shows a real 0% as 0%', () => {
    expect(rangeMetricsSummary({ ...base, cpu_pct: 0, memory_pct: 0 })).toContain('CPU 0% · Memory 0%');
  });

  it('marks a partial reading', () => {
    expect(rangeMetricsSummary({ ...base, cpu_pct: 1, partial: true })).toMatch(/\(partial\)$/);
  });

  it('shows no figures from an event recorded before metrics were measured', () => {
    const old: TelemetryEvent = { event_type: 'range_metrics', cpu_pct: 47.3, memory_pct: 61.2 };
    expect(rangeMetricsSummary(old)).toBe('Not measured (recorded before metrics came from the hypervisor)');
  });

  it('is empty for any other event', () => {
    expect(rangeMetricsSummary({ event_type: 'process_exec', cpu_pct: 5 })).toBe('');
  });
});

describe('telemetrySearchError', () => {
  it('shows the API reason for a query outside the grammar', () => {
    const err = new HttpErrorResponse({
      status: 422,
      error: { detail: 'Invalid search query: invalid field name \'_index\'' },
    });
    expect(telemetrySearchError(err)).toBe('Invalid search query: invalid field name \'_index\'');
  });

  it('falls back for a 422 from FastAPI validation (detail is a list)', () => {
    const err = new HttpErrorResponse({ status: 422, error: { detail: [{ msg: 'too long' }] } });
    expect(telemetrySearchError(err)).toBe('Invalid search query');
  });

  it('says the search failed for anything else', () => {
    expect(telemetrySearchError(new HttpErrorResponse({ status: 502 }))).toBe('Search failed');
    expect(telemetrySearchError(undefined)).toBe('Search failed');
  });
});

describe('TelemetryComponent', () => {
  let fixture: ComponentFixture<TelemetryComponent>;
  let component: TelemetryComponent;
  let api: jasmine.SpyObj<ApiService>;

  const ranges = [{ id: 'r1', name: 'Range A' }] as RangeSummary[];
  const tagged: TelemetryEvent = {
    '@timestamp': '2026-10-07T10:00:00Z',
    event_type: 'process_exec',
    hostname: 'ws-01',
    mitre_technique: ['T1059'],
  };

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', ['listRanges', 'searchTelemetry']);
    api.listRanges.and.returnValue(of(ranges));

    await TestBed.configureTestingModule({
      imports: [TelemetryComponent, NoopAnimationsModule],
      providers: [{ provide: ApiService, useValue: api }],
    }).compileComponents();

    fixture = TestBed.createComponent(TelemetryComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => fixture.destroy());

  it('loads the ranges to pick from', () => {
    expect(component.ranges()).toEqual(ranges);
  });

  it('shows an ATT&CK column', () => {
    expect(component.columns).toContain('mitre_technique');
  });

  it('does not search before a range is chosen', () => {
    component.search();
    expect(api.searchTelemetry).not.toHaveBeenCalled();
  });

  it('sends the range and query, and keeps the tagged events', () => {
    api.searchTelemetry.and.returnValue(of({ hits: { hits: [{ _source: tagged }] } }));
    component.selectedRangeId = 'r1';
    component.query = 'mitre_technique:T1059';
    component.search();
    expect(api.searchTelemetry).toHaveBeenCalledWith('r1', 'mitre_technique:T1059');
    expect(component.events()).toEqual([tagged]);
    expect(component.searchError()).toBe('');
    expect(component.techniques(component.events()[0])).toBe('T1059');
  });

  it('renders a range_metrics row with "Not reported" for a missing figure', () => {
    const metrics: TelemetryEvent = {
      '@timestamp': '2026-10-11T10:00:00Z', event_type: 'range_metrics', source: 'vsphere',
      vm_count: 2, vms_powered_on: 2, memory_pct: 40,
    };
    api.searchTelemetry.and.returnValue(of({ hits: { hits: [{ _source: metrics }] } }));
    component.selectedRangeId = 'r1';
    component.search();
    fixture.detectChanges();

    const row: HTMLElement | null = fixture.nativeElement.querySelector('tr.mat-mdc-row');
    expect(row?.textContent).toContain('CPU Not reported · Memory 40%');
    expect(row?.textContent).not.toContain('CPU 0%');
  });

  it('shows the 422 reason instead of "no events matched"', () => {
    api.searchTelemetry.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422,
      error: { detail: 'Invalid search query: wildcards are only allowed at the end of a value' },
    })));
    component.selectedRangeId = 'r1';
    component.query = 'cmd:*admin';
    component.search();
    fixture.detectChanges();

    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('wildcards are only allowed');
    expect(el.querySelector('.empty-state')).toBeNull();
    expect(component.events()).toEqual([]);
  });

  it('clears the error on the next good search', () => {
    api.searchTelemetry.and.returnValue(throwError(() => new HttpErrorResponse({ status: 502 })));
    component.selectedRangeId = 'r1';
    component.search();
    expect(component.searchError()).toBe('Search failed');

    api.searchTelemetry.and.returnValue(of({ hits: { hits: [] } }));
    component.search();
    expect(component.searchError()).toBe('');
  });
});
