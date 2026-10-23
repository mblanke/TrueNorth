import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type ContentItem = S['ContentItemOut'];
export type ContentItemDetail = S['ContentItemDetailOut'];
export type ContentItemList = S['ContentItemListOut'];
export type ContentItemBrief = S['ContentItemBrief'];
export type ContentTheme = S['ThemeOut'];
export type ContentCycle = S['CycleOut'];

/**
 * The content clock (ADR 0014 decisions 22-24): every course, module, scenario, range
 * template and golden template on its review-due date, its stale reasons, and the review
 * actions. Every call goes through this client so the auth interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class ContentReviewApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/content-review`;

  /** Items due in [start, end); `themeIds` filters ('none': untagged). */
  list(start: Date, end: Date, themeIds: string[] = []): Observable<ContentItemList> {
    let params = new HttpParams().set('start', start.toISOString()).set('end', end.toISOString());
    for (const t of themeIds) params = params.append('theme_id', t);
    return this.http.get<ContentItemList>(`${this.base}/items`, { params });
  }
  stale(): Observable<ContentItem[]> {
    return this.http.get<ContentItem[]>(`${this.base}/stale`);
  }
  get(id: string): Observable<ContentItemDetail> {
    return this.http.get<ContentItemDetail>(`${this.base}/items/${id}`);
  }
  markReviewed(id: string, note = ''): Observable<ContentItemDetail> {
    return this.http.post<ContentItemDetail>(`${this.base}/items/${id}/review`, { note });
  }
  assignReviewer(id: string, reviewerId: string | null): Observable<ContentItemDetail> {
    return this.http.put<ContentItemDetail>(`${this.base}/items/${id}/reviewer`, { reviewer_id: reviewerId });
  }
  retire(id: string, reason: string): Observable<ContentItemDetail> {
    return this.http.post<ContentItemDetail>(`${this.base}/items/${id}/retire`, { reason });
  }
  themes(): Observable<ContentTheme[]> {
    return this.http.get<ContentTheme[]>(`${this.base}/themes`);
  }
  cycles(): Observable<ContentCycle[]> {
    return this.http.get<ContentCycle[]>(`${this.base}/cycles`);
  }
}
