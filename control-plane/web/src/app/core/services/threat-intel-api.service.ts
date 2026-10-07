import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type ThreatFeed = S['ThreatIntelFeedOut'];
export type ThreatFeedIn = S['ThreatIntelFeedIn'];
export type FeedPull = S['FeedPullOut'];
export type ThreatIndicator = S['ThreatIndicatorOut'];

/**
 * Threat intelligence feeds (/threat-intel): list and create feeds, pull one now from its
 * URL, or upload its content (CSV). A pull answers what it did: created, updated,
 * deactivated and rejected rows.
 */
@Injectable({ providedIn: 'root' })
export class ThreatIntelApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/threat-intel`;

  feeds(): Observable<ThreatFeed[]> {
    return this.http.get<ThreatFeed[]>(`${this.base}/feeds`);
  }
  createFeed(body: ThreatFeedIn): Observable<ThreatFeed> {
    return this.http.post<ThreatFeed>(`${this.base}/feeds`, body);
  }
  deleteFeed(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/feeds/${id}`);
  }
  pull(id: string): Observable<FeedPull> {
    return this.http.post<FeedPull>(`${this.base}/feeds/${id}/pull`, {});
  }
  upload(id: string, file: File): Observable<FeedPull> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<FeedPull>(`${this.base}/feeds/${id}/upload`, form);
  }
  indicators(feedId: string, limit = 50): Observable<ThreatIndicator[]> {
    return this.http.get<ThreatIndicator[]>(`${this.base}/feeds/${feedId}/indicators?limit=${limit}`);
  }
}
