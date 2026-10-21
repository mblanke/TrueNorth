import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type RangeCredentials = S['RangeCredentialsOut'];
export type RangeCredential = S['RangeCredentialOut'];
export type RevealedCredential = S['RevealedCredentialOut'];
export type RotatedCredential = S['RotateOut'];
export type LabCredentials = S['LabCredentialsOut'];
export type TemplateIssue = S['TemplateIssueOut'];

/** Where a panel's accounts come from: a range, or a Student's own lab session. */
export type CredentialSource =
  | { kind: 'range'; rangeId: string }
  | { kind: 'lab'; sessionId: string; token?: string };

/**
 * A range's machine accounts (ADR 0012, ADR 0013 §3).
 *
 * The list never carries a password: each one is revealed on its own
 * (`POST .../credentials/{id}/reveal`), and every reveal is audited server-side, so
 * callers reveal only when the person asks. The API decides who sees what: staff every
 * account, a Student only the accounts handed to them on a range they take part in or
 * their own lab (404 otherwise), observers 403. Admins rotate an account (`.../rotate`).
 */
@Injectable({ providedIn: 'root' })
export class RangeCredentialsApiService {
  private http = inject(HttpClient);
  private base = environment.apiUrl;

  list(source: CredentialSource): Observable<{ accounts: RangeCredential[]; scope?: string }> {
    if (source.kind === 'range') {
      return this.http.get<RangeCredentials>(`${this.base}/ranges/${source.rangeId}/credentials`);
    }
    return this.http.get<LabCredentials>(this.labPath(source, 'credentials'), { headers: this.labHeaders(source) });
  }

  reveal(source: CredentialSource, credentialId: string): Observable<RevealedCredential> {
    if (source.kind === 'range') {
      return this.http.post<RevealedCredential>(
        `${this.base}/ranges/${source.rangeId}/credentials/${credentialId}/reveal`, {});
    }
    return this.http.post<RevealedCredential>(
      this.labPath(source, `credentials/${credentialId}/reveal`), {}, { headers: this.labHeaders(source) });
  }

  rotate(rangeId: string, credentialId: string, reason: string): Observable<RotatedCredential> {
    return this.http.post<RotatedCredential>(
      `${this.base}/ranges/${rangeId}/credentials/${credentialId}/rotate`, { reason });
  }

  /** Templates ADR 0013 no longer accepts (template:update; 403 otherwise). */
  templateIssues(): Observable<TemplateIssue[]> {
    return this.http.get<TemplateIssue[]>(`${this.base}/range-credentials/template-issues`);
  }

  private labPath(source: { sessionId: string; token?: string }, rest: string): string {
    const root = source.token ? 'lab-access' : 'lab-sessions';
    return `${this.base}/${root}/${source.sessionId}/${rest}`;
  }

  private labHeaders(source: CredentialSource): HttpHeaders {
    return source.kind === 'lab' && source.token
      ? new HttpHeaders({ 'X-Lab-Token': source.token })
      : new HttpHeaders();
  }
}
