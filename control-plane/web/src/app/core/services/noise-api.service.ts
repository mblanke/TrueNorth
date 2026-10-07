import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import type { components } from '../api/schema';
import { ApiService } from './api.service';

type S = components['schemas'];

export type NoiseProfile = S['NoiseProfileOut'];
export type NoiseProfileUpdate = S['ProfileIn'];
export type NoisePresets = S['NoisePresetsOut'];
export type NoiseAgent = S['NoiseAgentOut'];
export type NoisePersona = S['NoisePersonaOut'];
export type NoisePlan = S['NoisePlanOut'];
export type NoiseActivity = S['NoiseActivityOut'];
export type NoiseStats = S['NoiseStatsOut'];
export type NoiseDeploy = S['NoiseDeployOut'];

/**
 * Background noise, the white cell's side (`/noise/ranges/{id}/...`, NOISE_READ and
 * NOISE_CONTROL). Through the shared ApiService, typed from the generated contract.
 * The agents' channel (`/noise/agent/...`) is not for the browser.
 */
@Injectable({ providedIn: 'root' })
export class NoiseApiService {
  private readonly api = inject(ApiService);

  private path(rangeId: string, rest = ''): string {
    return `/noise/ranges/${encodeURIComponent(rangeId)}${rest}`;
  }

  presets(): Observable<NoisePresets> {
    return this.api.get<NoisePresets>('/noise/presets');
  }
  profile(rangeId: string): Observable<NoiseProfile> {
    return this.api.get<NoiseProfile>(this.path(rangeId));
  }
  /** Only the fields sent change: the dial, enabled (start/stop), paused, overrides, targets. */
  update(rangeId: string, body: NoiseProfileUpdate): Observable<NoiseProfile> {
    return this.api.put<NoiseProfile>(this.path(rangeId), body);
  }
  agents(rangeId: string): Observable<NoiseAgent[]> {
    return this.api.get<NoiseAgent[]>(this.path(rangeId, '/agents'));
  }
  personas(rangeId: string): Observable<NoisePersona[]> {
    return this.api.get<NoisePersona[]>(this.path(rangeId, '/personas'));
  }
  plan(rangeId: string, node: string, minutes = 15): Observable<NoisePlan> {
    const q = `?node=${encodeURIComponent(node)}&minutes=${minutes}`;
    return this.api.get<NoisePlan>(this.path(rangeId, `/plan${q}`));
  }
  activity(rangeId: string, opts: { limit?: number; lookalike?: boolean } = {}): Observable<NoiseActivity[]> {
    const q = new URLSearchParams({ limit: String(opts.limit ?? 100) });
    if (opts.lookalike !== undefined) q.set('lookalike', String(opts.lookalike));
    return this.api.get<NoiseActivity[]>(this.path(rangeId, `/activity?${q}`));
  }
  stats(rangeId: string, minutes = 60): Observable<NoiseStats> {
    return this.api.get<NoiseStats>(this.path(rangeId, `/stats?minutes=${minutes}`));
  }
  /** `dryRun` shows what a deploy would do and changes nothing. */
  deploy(rangeId: string, dryRun: boolean): Observable<NoiseDeploy> {
    const body: S['DeployIn'] = { dry_run: dryRun, refresh_targets: true };
    return this.api.post<NoiseDeploy>(this.path(rangeId, '/deploy'), body);
  }
}
