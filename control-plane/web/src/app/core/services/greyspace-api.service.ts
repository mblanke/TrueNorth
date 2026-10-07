import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type GreyspaceBlock = S['GreyspaceBlock'];
export type GreyspaceStatus = S['GreyspaceStatusOut'];
export type GreyspaceConfig = S['GreyspaceConfigOut'];
export type GreyspaceCorpus = S['CorpusSummary'];

/**
 * Greyspace, the simulated internet a range can attach (ADR 0007): corpus tiers, and per
 * range the block, its status and the generated stack's summary.
 */
@Injectable({ providedIn: 'root' })
export class GreyspaceApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  corpora(): Observable<GreyspaceCorpus[]> {
    return this.http.get<GreyspaceCorpus[]>(`${this.base}/greyspace/corpora`);
  }
  status(rangeId: string): Observable<GreyspaceStatus> {
    return this.http.get<GreyspaceStatus>(`${this.base}/ranges/${encodeURIComponent(rangeId)}/greyspace`);
  }
  /** Attach or replace the block. `null` attaches the template's block, else the defaults. */
  attach(rangeId: string, block: Partial<GreyspaceBlock> | null): Observable<GreyspaceStatus> {
    return this.http.put<GreyspaceStatus>(`${this.base}/ranges/${encodeURIComponent(rangeId)}/greyspace`, block);
  }
  detach(rangeId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/ranges/${encodeURIComponent(rangeId)}/greyspace`);
  }
  config(rangeId: string): Observable<GreyspaceConfig> {
    return this.http.get<GreyspaceConfig>(`${this.base}/ranges/${encodeURIComponent(rangeId)}/greyspace/config`);
  }
}
