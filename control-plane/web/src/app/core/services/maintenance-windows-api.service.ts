import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type MaintenanceWindow = S['MaintenanceWindowOut'];
export type MaintenanceWindowIn = S['MaintenanceWindowIn'];
export type MaintenanceWindowList = S['MaintenanceWindowListOut'];
export type WindowScope = S['WindowScope'];
export type RangeDetail = S['RangeOut'];

/**
 * Maintenance windows and emergency teardown (ADR 0013 §7, range certification S7).
 * Inside a window, provision, reset/restore and normal destroy are refused by the API;
 * automated cleanup and an admin's emergency teardown still run. Every call goes through
 * this client so the auth interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class MaintenanceWindowsApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/maintenance-windows`;

  /** The caller's tenant's windows and the platform-wide ones (schedule:read). */
  list(includePast = false, limit = 200): Observable<MaintenanceWindowList> {
    const params = new HttpParams().set('include_past', includePast).set('limit', limit);
    return this.http.get<MaintenanceWindowList>(this.base, { params });
  }
  /** In effect now (range:read): the banner. With a range, only the windows that cover it. */
  active(rangeId?: string): Observable<MaintenanceWindow[]> {
    const params = rangeId ? new HttpParams().set('range_id', rangeId) : undefined;
    return this.http.get<MaintenanceWindow[]>(`${this.base}/active`, { params });
  }
  create(body: MaintenanceWindowIn): Observable<MaintenanceWindow> {
    return this.http.post<MaintenanceWindow>(this.base, body);
  }
  update(id: string, body: MaintenanceWindowIn): Observable<MaintenanceWindow> {
    return this.http.put<MaintenanceWindow>(`${this.base}/${id}`, body);
  }
  remove(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }

  /** range:admin. Runs even inside a window; the reason is audited and recorded. */
  emergencyTeardown(rangeId: string, reason: string): Observable<RangeDetail> {
    const body: S['EmergencyTeardownIn'] = { confirm_range_id: rangeId, reason };
    return this.http.post<RangeDetail>(`${environment.apiUrl}/ranges/${rangeId}/emergency-teardown`, body);
  }
}
