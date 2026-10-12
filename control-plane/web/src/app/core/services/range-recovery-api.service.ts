import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type RangeDetail = S['RangeOut'];
export type RangeOperation = S['RangeOperationOut'];
export type RangeLeaseStatus = S['LeaseStatusOut'];
export type ForceReleaseResult = S['ForceReleaseOut'];
export type NetworkReservation = S['NetworkReservationOut'];
export type RangeSnapshot = S['SnapshotOut'];

/** Operation statuses that are still in flight (app/range_ops/models.py IN_FLIGHT). */
export const OPERATION_IN_FLIGHT = ['pending', 'dispatched'];
/** Snapshot states the worker is still writing (routers/ranges.py _SNAPSHOT_BUSY). */
export const SNAPSHOT_BUSY = ['creating', 'restoring'];

/**
 * Range recovery and snapshots (R0-B): operations, the lease and its force-release,
 * network reservations, abandoning an operation or a whole range, snapshots.
 * Every call goes through this client so the auth interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class RangeRecoveryApiService {
  private http = inject(HttpClient);
  private base = `${environment.apiUrl}/ranges`;

  getRange(id: string): Observable<RangeDetail> {
    return this.http.get<RangeDetail>(`${this.base}/${id}`);
  }

  listOperations(id: string): Observable<RangeOperation[]> {
    return this.http.get<RangeOperation[]>(`${this.base}/${id}/operations`);
  }
  getOperation(id: string, operationId: string): Observable<RangeOperation> {
    return this.http.get<RangeOperation>(`${this.base}/${id}/operations/${operationId}`);
  }
  abandonOperation(id: string, operationId: string): Observable<RangeOperation> {
    return this.http.post<RangeOperation>(`${this.base}/${id}/operations/${operationId}/abandon`, {});
  }

  getLease(id: string): Observable<RangeLeaseStatus> {
    return this.http.get<RangeLeaseStatus>(`${this.base}/${id}/lease`);
  }
  forceReleaseLease(id: string, reason: string): Observable<ForceReleaseResult> {
    return this.http.post<ForceReleaseResult>(`${this.base}/${id}/lease/force-release`, {
      confirm_range_id: id,
      reason,
    });
  }

  /** Platform administrators only: records the range destroyed without touching the hypervisor. */
  abandonRange(id: string, reason: string): Observable<RangeDetail> {
    return this.http.post<RangeDetail>(`${this.base}/${id}/abandon`, { reason });
  }

  listNetworkReservations(id: string): Observable<NetworkReservation[]> {
    return this.http.get<NetworkReservation[]>(`${this.base}/${id}/network-reservations`);
  }

  listSnapshots(id: string): Observable<RangeSnapshot[]> {
    return this.http.get<RangeSnapshot[]>(`${this.base}/${id}/snapshots`);
  }
  createSnapshot(id: string, name: string, description?: string): Observable<RangeSnapshot> {
    return this.http.post<RangeSnapshot>(`${this.base}/${id}/snapshots`, { name, description: description || null });
  }
  deleteSnapshot(id: string, snapshotId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}/snapshots/${snapshotId}`);
  }
  restoreSnapshot(id: string, snapshotId: string): Observable<RangeDetail> {
    return this.http.post<RangeDetail>(`${this.base}/${id}/snapshots/${snapshotId}/restore`, {});
  }
}
