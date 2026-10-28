import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type UnitReadiness = S['UnitReadinessOut'];
export type ReadinessClass = S['ReadinessClassOut'];
export type ReadinessStudent = S['ReadinessStudentOut'];

/** The page's filters; an empty string is "not set" (the API's defaults apply). */
export interface ReadinessFilter {
  dateFrom: string; // yyyy-mm-dd
  dateTo: string;
  unit: string;
}

function params(f: ReadinessFilter): HttpParams {
  let p = new HttpParams();
  if (f.dateFrom) p = p.set('date_from', f.dateFrom);
  if (f.dateTo) p = p.set('date_to', f.dateTo);
  if (f.unit.trim()) p = p.set('unit', f.unit.trim());
  return p;
}

/**
 * The unit readiness page (ADR 0014 decision 7; readiness:read). The API keeps it to the
 * caller's own tenant and only reads; every call goes through this client so the auth
 * interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class ReadinessApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/readiness`;

  get(f: ReadinessFilter): Observable<UnitReadiness> {
    return this.http.get<UnitReadiness>(this.base, { params: params(f) });
  }
  /** One row per Student per class (formula cells defused by the API). Audited. */
  exportCsv(f: ReadinessFilter): Observable<string> {
    return this.http.get(`${this.base}/export.csv`, { params: params(f), responseType: 'text' });
  }
  /** The same as a PDF. Audited. */
  exportPdf(f: ReadinessFilter): Observable<Blob> {
    return this.http.get(`${this.base}/export.pdf`, { params: params(f), responseType: 'blob' });
  }
}
