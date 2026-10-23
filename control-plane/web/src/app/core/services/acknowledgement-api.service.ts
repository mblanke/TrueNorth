import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type AcknowledgementCurrent = S['AcknowledgementCurrentOut'];
export type AcknowledgementVersion = S['AcknowledgementVersionOut'];

/**
 * The rules of use (ADR 0014 decision 3, control-plane/api/app/acknowledgement).
 *
 * `current` and `sign` answer even while the caller must still sign; every other API call
 * answers 403 `acknowledgement_required` until they have. Only the platform administrator
 * manages versions; any admin exports their organisation's signatures.
 */
@Injectable({ providedIn: 'root' })
export class AcknowledgementApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/acknowledgement`;

  current(): Observable<AcknowledgementCurrent> {
    return this.http.get<AcknowledgementCurrent>(`${this.base}/current`);
  }
  sign(versionId: string): Observable<AcknowledgementCurrent> {
    return this.http.post<AcknowledgementCurrent>(`${this.base}/sign`, { version_id: versionId });
  }

  versions(): Observable<AcknowledgementVersion[]> {
    return this.http.get<AcknowledgementVersion[]>(`${this.base}/versions`);
  }
  createDraft(text: string): Observable<AcknowledgementVersion> {
    return this.http.post<AcknowledgementVersion>(`${this.base}/versions`, { text });
  }
  /** Drafts only: a published version never changes (409). */
  updateDraft(id: string, text: string): Observable<AcknowledgementVersion> {
    return this.http.put<AcknowledgementVersion>(`${this.base}/versions/${id}`, { text });
  }
  /** Publishing makes it the next version, which everyone signs before going on. */
  publish(id: string): Observable<AcknowledgementVersion> {
    return this.http.post<AcknowledgementVersion>(`${this.base}/versions/${id}/publish`, {});
  }
  /** CSV of signatures: the caller's organisation, or every one for the platform administrator. */
  exportSignatures(): Observable<Blob> {
    return this.http.get(`${this.base}/signatures/export`, { responseType: 'blob' });
  }
}
