// Author: Marc-Andre Blanke
// Created: 2026-11-28T07:00:00Z
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type CostReport = S['CostReportOut'];
export type CostRow = S['CostRow'];
export type Figures = S['Figures'];
export type StudentCost = S['StudentCostOut'];
export type UsageInterval = S['UsageIntervalOut'];
export type RateCard = S['RateCardOut'];
export type RateCardIn = S['RateCardIn'];
export type GroupBy = 'student' | 'course' | 'class' | 'tenant';

export interface CostQuery {
  from: string; // ISO instant
  to: string;
  groupBy: GroupBy;
  tenantId?: string | null; // platform administrator only; none = every tenant
}

/**
 * Usage and cost (ADR 0021): what each Student costs to run on the range, at the size each
 * range was built and the rate card in force. Admins only (usage:read); a new rate card
 * needs usage:admin, the platform default the platform administrator.
 */
@Injectable({ providedIn: 'root' })
export class UsageApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/usage`;

  private params(q: Omit<CostQuery, 'groupBy'> & { groupBy?: GroupBy }): HttpParams {
    let p = new HttpParams().set('from', q.from).set('to', q.to);
    if (q.groupBy) p = p.set('group_by', q.groupBy);
    if (q.tenantId) p = p.set('tenant_id', q.tenantId);
    return p;
  }

  cost(q: CostQuery): Observable<CostReport> {
    return this.http.get<CostReport>(`${this.base}/cost`, { params: this.params(q) });
  }

  /** The same rows as CSV, fetched through the client so the auth header goes with it. */
  costCsv(q: CostQuery): Observable<Blob> {
    return this.http.get(`${this.base}/cost.csv`, { params: this.params(q), responseType: 'blob' });
  }

  student(userId: string, from: string, to: string): Observable<StudentCost> {
    return this.http.get<StudentCost>(`${this.base}/cost/students/${userId}`, { params: this.params({ from, to }) });
  }

  rateCards(): Observable<RateCard[]> {
    return this.http.get<RateCard[]>(`${this.base}/rate-cards`);
  }

  createRateCard(body: RateCardIn): Observable<RateCard> {
    return this.http.post<RateCard>(`${this.base}/rate-cards`, body);
  }
}
