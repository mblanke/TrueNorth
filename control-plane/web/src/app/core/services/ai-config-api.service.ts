import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';
import { AIBackendConfig, AIFleetSummary, AIModelRoute } from '../models';

type S = components['schemas'];

/** GET /ai-config/models — untyped in the contract. */
export interface AvailableModels {
  models: { name: string; node: string }[];
  count: number;
}

/** One node of POST /ai-config/backends/{id}/discover — untyped in the contract. */
export interface DiscoverNodeResult {
  node_name: string;
  url: string;
  online: boolean;
  version: string | null;
  gpu_model: string | null;
  gpu_vram_gb: number | null;
  model_count: number;
  models: {
    name: string;
    size_bytes: number;
    family: string;
    parameter_size: string;
    quantization: string;
  }[];
  running: string[];
}

/** AI backends, model routes and fleet discovery. Same base-URL convention as ApiService. */
@Injectable({ providedIn: 'root' })
export class AiConfigApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  backends(): Observable<AIBackendConfig[]> {
    return this.http.get<AIBackendConfig[]>(`${this.base}/ai-config/backends`);
  }
  createBackend(body: S['AIBackendConfigIn']): Observable<AIBackendConfig> {
    return this.http.post<AIBackendConfig>(`${this.base}/ai-config/backends`, body);
  }
  updateBackend(id: string, body: S['AIBackendConfigUpdate']): Observable<AIBackendConfig> {
    return this.http.patch<AIBackendConfig>(`${this.base}/ai-config/backends/${id}`, body);
  }
  deleteBackend(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/ai-config/backends/${id}`);
  }
  setPrimaryBackend(id: string): Observable<unknown> {
    return this.http.post<unknown>(`${this.base}/ai-config/backends/${id}/set-primary`, {});
  }
  discoverFleet(backendId: string): Observable<{ scanned: number; results: DiscoverNodeResult[] }> {
    return this.http.post<{ scanned: number; results: DiscoverNodeResult[] }>(
      `${this.base}/ai-config/backends/${backendId}/discover`, {},
    );
  }

  routes(): Observable<AIModelRoute[]> {
    return this.http.get<AIModelRoute[]>(`${this.base}/ai-config/routes`);
  }
  createRoute(body: S['AIModelRouteIn']): Observable<AIModelRoute> {
    return this.http.post<AIModelRoute>(`${this.base}/ai-config/routes`, body);
  }
  updateRoute(id: string, body: S['AIModelRouteUpdate']): Observable<AIModelRoute> {
    return this.http.patch<AIModelRoute>(`${this.base}/ai-config/routes/${id}`, body);
  }
  deleteRoute(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/ai-config/routes/${id}`);
  }

  summary(): Observable<AIFleetSummary> {
    return this.http.get<AIFleetSummary>(`${this.base}/ai-config/summary`);
  }
  models(): Observable<AvailableModels> {
    return this.http.get<AvailableModels>(`${this.base}/ai-config/models`);
  }
  /** Prompt and model travel as query parameters with an empty body, as the API expects. */
  testGenerate(prompt: string, model: string): Observable<unknown> {
    return this.http.post<unknown>(`${this.base}/ai-config/test-generate`, null, {
      params: { prompt, model },
    });
  }
}
