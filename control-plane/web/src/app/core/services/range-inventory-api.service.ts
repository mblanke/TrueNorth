import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type RangeInventoryChecks = S['RangeInventoryChecksOut'];
export type TeardownCheck = S['TeardownCheckOut'];
export type DriftCheck = S['DriftCheckOut'];
export type DriftFinding = S['DriftFindingOut'];
export type InventoryItem = S['InventoryItemOut'];
export type ConfirmItem = S['ConfirmItemIn'];
export type ConfirmUnidentifiedResult = S['ConfirmUnidentifiedOut'];

/**
 * Range certification S5 (ADR 0013 §5): the range's last teardown verification and its
 * drift check (staff, `range:certification_read`), and an administrator's confirmation of
 * unidentified items by their typed-back managed-object ids (`range:admin`). The server
 * checks every typed id against the listed set; nothing is deleted by a mark alone.
 */
@Injectable({ providedIn: 'root' })
export class RangeInventoryApiService {
  private http = inject(HttpClient);
  private base = `${environment.apiUrl}/ranges`;

  get(rangeId: string): Observable<RangeInventoryChecks> {
    return this.http.get<RangeInventoryChecks>(`${this.base}/${rangeId}/inventory-checks`);
  }

  /** Each item's `confirm_moid` must repeat its `moid` exactly; a reason is required. */
  confirmUnidentified(rangeId: string, items: ConfirmItem[], reason: string): Observable<ConfirmUnidentifiedResult> {
    return this.http.post<ConfirmUnidentifiedResult>(`${this.base}/${rangeId}/teardown/confirm-unidentified`,
      { items, reason });
  }
}
