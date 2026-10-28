import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type ProvenStatus = S['ProvenStatusOut'];
export type ProvenCondition = S['ProvenConditionOut'];
export type PortPreview = S['PortPreviewOut'];
export type PortPreviewItem = S['PortPreviewItemOut'];
export type PortImport = S['PortImportOut'];
export type PortSigningKey = S['PortSigningKeyOut'];
export type PortItemType = ProvenStatus['item_type'];

/** The content types that are ported (and so get a Proven chip). */
export const PORTABLE_TYPES: readonly string[] = ['course', 'range_template', 'scenario', 'exercise_template'];

export interface PortTarget {
  tenantId?: string | null;
  shared?: boolean;
}

/**
 * The content port (docs/content-port.md): whether a course, range template or scenario is
 * proven, and, for the platform administrator, exporting proven content as a signed bundle
 * and previewing and importing one. Every call goes through this client so the auth
 * interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class ContentPortApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/content-port`;

  proven(itemType: string, itemId: string): Observable<ProvenStatus> {
    return this.http.get<ProvenStatus>(`${this.base}/items/${itemType}/${itemId}/proven`);
  }
  /** Platform administrator: every item that could be ported, with its status. */
  items(): Observable<ProvenStatus[]> {
    return this.http.get<ProvenStatus[]>(`${this.base}/items`);
  }
  signingKey(): Observable<PortSigningKey> {
    return this.http.get<PortSigningKey>(`${this.base}/signing-key`);
  }
  /** The signed bundle, as the exact text the API sent (409 lists what is not proven). */
  exportBundle(items: { item_type: PortItemType; item_id: string }[]): Observable<string> {
    return this.http.post(`${this.base}/export`, { items }, { responseType: 'text' });
  }
  preview(file: File, target: PortTarget = {}): Observable<PortPreview> {
    return this.http.post<PortPreview>(`${this.base}/preview`, this.form(file, target, []));
  }
  importBundle(file: File, target: PortTarget = {}, overwrite: string[] = []): Observable<PortImport> {
    return this.http.post<PortImport>(`${this.base}/import`, this.form(file, target, overwrite));
  }

  private form(file: File, target: PortTarget, overwrite: string[]): FormData {
    const body = new FormData();
    body.append('file', file, file.name);
    if (target.tenantId) body.append('target_tenant_id', target.tenantId);
    if (target.shared) body.append('shared', 'true');
    for (const key of overwrite) body.append('overwrite', key);
    return body;
  }
}
