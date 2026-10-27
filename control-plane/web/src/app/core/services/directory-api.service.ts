import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';
import {
  ADSyncStatus, AuthZonePolicy, Coalition, Nation, OrganizationalUnit, SecurityGroup,
} from '../models';

type S = components['schemas'];

/** One QSP qualification (GET /qsp/qualifications). */
export type Qualification = S['QualificationOut'];

/** The AD status sync panel (GET /ad-status-sync) and one run (#290). */
export type AdStatusSyncOverview = S['AdStatusSyncOverviewOut'];
export type AdStatusSyncRun = S['AdStatusSyncRunOut'];

/**
 * Directory reference data and administration: nations, coalitions, OUs, security
 * groups, auth zones, AD sync and the QSP qualification catalogue.
 * Same base-URL convention as ApiService.
 */
@Injectable({ providedIn: 'root' })
export class DirectoryApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  nations(): Observable<Nation[]> {
    return this.http.get<Nation[]>(`${this.base}/directory/nations`);
  }
  coalitions(): Observable<Coalition[]> {
    return this.http.get<Coalition[]>(`${this.base}/directory/coalitions`);
  }

  ouTree(): Observable<OrganizationalUnit[]> {
    return this.http.get<OrganizationalUnit[]>(`${this.base}/directory/ous/tree`);
  }
  createOu(body: S['OUIn']): Observable<S['OUOut']> {
    return this.http.post<S['OUOut']>(`${this.base}/directory/ous`, body);
  }
  updateOu(id: string, body: S['OUUpdate']): Observable<S['OUOut']> {
    return this.http.patch<S['OUOut']>(`${this.base}/directory/ous/${id}`, body);
  }

  securityGroups(): Observable<SecurityGroup[]> {
    return this.http.get<SecurityGroup[]>(`${this.base}/directory/groups`);
  }
  createSecurityGroup(body: S['SecurityGroupIn']): Observable<SecurityGroup> {
    return this.http.post<SecurityGroup>(`${this.base}/directory/groups`, body);
  }
  updateSecurityGroup(id: string, body: S['SecurityGroupUpdate']): Observable<SecurityGroup> {
    return this.http.patch<SecurityGroup>(`${this.base}/directory/groups/${id}`, body);
  }

  authZones(): Observable<AuthZonePolicy[]> {
    return this.http.get<AuthZonePolicy[]>(`${this.base}/auth-zones`);
  }
  updateAuthZone(id: string, body: S['AuthZonePolicyIn']): Observable<AuthZonePolicy> {
    return this.http.patch<AuthZonePolicy>(`${this.base}/auth-zones/${id}`, body);
  }

  /** The contract declares no response schema for the AD sync endpoints. */
  adSyncStatus(): Observable<ADSyncStatus> {
    return this.http.get<ADSyncStatus>(`${this.base}/ad-sync/status`);
  }
  triggerAdSync(): Observable<{ message?: string } & Record<string, unknown>> {
    return this.http.post<{ message?: string } & Record<string, unknown>>(`${this.base}/ad-sync/trigger`, {});
  }

  /** Nightly AD status sync: settings, recent runs, the latest run's accounts (admin, own tenant). */
  adStatusSync(): Observable<AdStatusSyncOverview> {
    return this.http.get<AdStatusSyncOverview>(`${this.base}/ad-status-sync`);
  }
  /** Run it now for this tenant; omit dryRun to follow the server's AD_STATUS_SYNC_DRY_RUN. */
  runAdStatusSync(dryRun?: boolean, overrideLimit = false): Observable<AdStatusSyncRun> {
    const body: S['AdStatusSyncRunIn'] = { override_limit: overrideLimit };
    if (dryRun !== undefined) body.dry_run = dryRun;
    return this.http.post<AdStatusSyncRun>(`${this.base}/ad-status-sync/runs`, body);
  }

  qualifications(): Observable<Qualification[]> {
    return this.http.get<Qualification[]>(`${this.base}/qsp/qualifications`);
  }
}
