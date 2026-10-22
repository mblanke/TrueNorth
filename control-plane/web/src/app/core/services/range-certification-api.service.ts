import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type RangeCertification = S['RangeCertificationOut'];
export type CertificationRecord = S['CertificationRecordOut'];
export type CertificationSection = S['CertificationSectionOut'];
export type CertificationCapture = S['CertificationCaptureOut'];
export type ArchivedCertification = S['ArchivedCertificationOut'];
export type CertificationEntry = S['CertificationEntryOut'];
export type IsolationCheck = S['IsolationCheckOut'];

/** One security zone's row of the isolation section's detail (worker/range_isolation.py). */
export interface IsolationZone {
  zone: string;
  cidr?: string;
  status: 'probed' | 'no_probe' | 'guest_op_failed' | 'budget_exhausted' | 'not_run' | string;
  probe?: { vm: string; family: string; account: string; admin: boolean; flagged?: boolean } | null;
  selftest?: { passed: boolean; targets?: string };
  ipv6?: Record<string, string>;
  verdict?: 'isolated' | 'not_isolated' | 'incomplete' | string;
  reasons?: string[];
  checks: { check: string; target: string; expect: string; result: string; passed?: boolean | null; note?: string }[];
}

/**
 * The Range Certification Record (ADR 0013 §6): GET /ranges/{id}/certification and its
 * signed JSON / PDF exports. Staff only (`range:certification_read`); Students and
 * observers get 403. Exports are audited server-side.
 */
@Injectable({ providedIn: 'root' })
export class RangeCertificationApiService {
  private http = inject(HttpClient);
  private base = `${environment.apiUrl}/ranges`;

  /**
   * The current record (the newest build's, or `deploymentId`'s) with each section's latest
   * entry; `history` adds every superseded entry. Earlier builds are listed in `archived`.
   */
  get(rangeId: string, opts: { deploymentId?: string; history?: boolean } = {}): Observable<RangeCertification> {
    const params: Record<string, string> = {};
    if (opts.deploymentId) params['deployment_id'] = opts.deploymentId;
    if (opts.history) params['history'] = 'true';
    return this.http.get<RangeCertification>(`${this.base}/${rangeId}/certification`, { params });
  }

  /** Append the evidence TrueNorth holds now (needs range:provision too). */
  capture(rangeId: string): Observable<CertificationCapture> {
    return this.http.post<CertificationCapture>(`${this.base}/${rangeId}/certification/capture`, {});
  }

  /**
   * Probe the range's isolation again from every zone (ADR 0013 §4; needs range:provision
   * and a ready range, else 409). The worker stores the result; the record shows it on its
   * next read. Never changes the range's state.
   */
  checkIsolation(rangeId: string): Observable<IsolationCheck> {
    return this.http.post<IsolationCheck>(`${this.base}/${rangeId}/certification/isolation-check`, {});
  }

  /** The signed JSON export (`json`) or its readable PDF (`pdf`), as a file. */
  export(rangeId: string, format: 'json' | 'pdf', deploymentId?: string): Observable<Blob> {
    const params: Record<string, string> = deploymentId ? { deployment_id: deploymentId } : {};
    const path = format === 'pdf' ? 'certification/export/pdf' : 'certification/export';
    return this.http.get(`${this.base}/${rangeId}/${path}`, { params, responseType: 'blob' });
  }
}
