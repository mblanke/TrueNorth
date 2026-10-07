import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { RangeSummary, TelemetryEvent } from '@core/models';
import { TelemetryComponent, telemetrySearchError, telemetryTechniques } from './telemetry.component';

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
