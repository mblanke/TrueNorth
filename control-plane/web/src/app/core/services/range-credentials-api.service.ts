import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type RangeCredentials = S['RangeCredentialsOut'];
export type RangeCredential = S['RangeCredentialOut'];

/**
 * A range's machine accounts (ADR 0012): GET /ranges/{id}/credentials.
 *
 * The API decides what comes back: staff see every account; a Student sees only the
 * accounts handed to them on a range they take part in (404 otherwise); observers get
 * 403. Every read is audited server-side, so callers fetch only when the person asks.
 */
@Injectable({ providedIn: 'root' })
export class RangeCredentialsApiService {
  private http = inject(HttpClient);
  private base = `${environment.apiUrl}/ranges`;

  getCredentials(rangeId: string): Observable<RangeCredentials> {
    return this.http.get<RangeCredentials>(`${this.base}/${rangeId}/credentials`);
  }
}
