import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';
import {
  HypervisorConnection, HypervisorNode, HypervisorSummary, NetworkDevice, NetworkSummary,
  StorageAppliance, StorageSummary,
} from '../models';

type S = components['schemas'];

/** discover / set-primary have no response_model in the contract; both handlers return
 *  a dict with a human-readable `message` (plus backend-specific counters). */
export type InfraActionResult = { message: string } & Record<string, unknown>;

/**
 * Physical infrastructure inventory: hypervisor connections and hosts, storage
 * appliances, network devices. Same base-URL convention as ApiService.
 */
@Injectable({ providedIn: 'root' })
export class InfrastructureApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  // ── Hypervisors ──────────────────────────────────────────
  connections(): Observable<HypervisorConnection[]> {
    return this.http.get<HypervisorConnection[]>(`${this.base}/hypervisors/connections`);
  }
  connectionNodes(id: string): Observable<HypervisorNode[]> {
    return this.http.get<HypervisorNode[]>(`${this.base}/hypervisors/connections/${id}/nodes`);
  }
  hypervisorSummary(): Observable<HypervisorSummary> {
    return this.http.get<HypervisorSummary>(`${this.base}/hypervisors/summary`);
  }
  createConnection(body: S['HypervisorConnectionIn']): Observable<HypervisorConnection> {
    return this.http.post<HypervisorConnection>(`${this.base}/hypervisors/connections`, body);
  }
  updateConnection(id: string, body: S['HypervisorConnectionUpdate']): Observable<HypervisorConnection> {
    return this.http.patch<HypervisorConnection>(`${this.base}/hypervisors/connections/${id}`, body);
  }
  deleteConnection(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/hypervisors/connections/${id}`);
  }
  testConnection(id: string): Observable<S['HypervisorTestResult']> {
    return this.http.post<S['HypervisorTestResult']>(`${this.base}/hypervisors/connections/${id}/test`, {});
  }
  discoverConnection(id: string): Observable<InfraActionResult> {
    return this.http.post<InfraActionResult>(`${this.base}/hypervisors/connections/${id}/discover`, {});
  }
  setPrimaryConnection(id: string): Observable<InfraActionResult> {
    return this.http.post<InfraActionResult>(`${this.base}/hypervisors/connections/${id}/set-primary`, {});
  }

  // ── Storage ──────────────────────────────────────────────
  appliances(): Observable<StorageAppliance[]> {
    return this.http.get<StorageAppliance[]>(`${this.base}/storage/appliances`);
  }
  storageSummary(): Observable<StorageSummary> {
    return this.http.get<StorageSummary>(`${this.base}/storage/summary`);
  }
  createAppliance(body: S['StorageApplianceIn']): Observable<StorageAppliance> {
    return this.http.post<StorageAppliance>(`${this.base}/storage/appliances`, body);
  }
  updateAppliance(id: string, body: S['StorageApplianceUpdate']): Observable<StorageAppliance> {
    return this.http.patch<StorageAppliance>(`${this.base}/storage/appliances/${id}`, body);
  }
  deleteAppliance(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/storage/appliances/${id}`);
  }

  // ── Network devices (the collection path has a trailing slash in the API) ──
  networkDevices(): Observable<NetworkDevice[]> {
    return this.http.get<NetworkDevice[]>(`${this.base}/network-devices/`);
  }
  networkSummary(): Observable<NetworkSummary> {
    return this.http.get<NetworkSummary>(`${this.base}/network-devices/summary`);
  }
  createNetworkDevice(body: S['NetworkDeviceIn']): Observable<NetworkDevice> {
    return this.http.post<NetworkDevice>(`${this.base}/network-devices/`, body);
  }
  updateNetworkDevice(id: string, body: S['NetworkDeviceUpdate']): Observable<NetworkDevice> {
    return this.http.patch<NetworkDevice>(`${this.base}/network-devices/${id}`, body);
  }
  deleteNetworkDevice(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/network-devices/${id}`);
  }
}
