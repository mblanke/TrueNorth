import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type ClassRecord = S['ClassOut'];
export type ClassIn = S['ClassIn'];
export type RollCallEntry = S['RollCallEntryOut'];
export type RollCallAddResult = S['RollCallAddOut'];
export type RollCallStatus = S['RollCallStatusOut'];
export type PendingAddition = S['PendingAdditionOut'];
export type AdditionDecision = S['AdditionDecisionOut'];
export type ReinstateResult = S['ReinstateOut'];

/**
 * Classes and their roll call (ADR 0014). The API decides who sees and changes what:
 * a class's lead or an admin changes it and its roll call; its assistants and observers
 * read it. Every call goes through this client so the auth interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class ClassesApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/classes`;

  /** The classes the caller leads, assists or observes (class:read). */
  mine(includeCancelled = false): Observable<ClassRecord[]> {
    const params = new HttpParams().set('include_cancelled', includeCancelled);
    return this.http.get<ClassRecord[]>(`${this.base}/mine`, { params });
  }
  /** Everything the caller may see: for an admin, every class of their organisation. */
  list(includeCancelled = false): Observable<ClassRecord[]> {
    const params = new HttpParams().set('include_cancelled', includeCancelled);
    return this.http.get<ClassRecord[]>(this.base, { params });
  }
  create(body: ClassIn): Observable<ClassRecord> {
    return this.http.post<ClassRecord>(this.base, body);
  }
  update(id: string, body: ClassIn): Observable<ClassRecord> {
    return this.http.put<ClassRecord>(`${this.base}/${id}`, body);
  }
  /** Cancels the class: it is kept on record, never deleted. */
  cancel(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }

  rollCall(id: string): Observable<RollCallEntry[]> {
    return this.http.get<RollCallEntry[]>(`${this.base}/${id}/roll-call`);
  }
  /** Pasted text: any mix of commas, semicolons, spaces and new lines. */
  addPasted(id: string, text: string): Observable<RollCallAddResult> {
    return this.http.post<RollCallAddResult>(`${this.base}/${id}/roll-call`, { emails: [], text });
  }
  addCsv(id: string, file: File): Observable<RollCallAddResult> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<RollCallAddResult>(`${this.base}/${id}/roll-call/csv`, form);
  }
  // ── Additions after approval that did not fit (ADR 0014 §17; schedule:approve) ──
  pendingAdditions(): Observable<PendingAddition[]> {
    return this.http.get<PendingAddition[]>(`${this.base}/pending-additions`);
  }
  approveAddition(id: string, entryId: string): Observable<AdditionDecision> {
    return this.http.post<AdditionDecision>(`${this.base}/${id}/roll-call/${entryId}/approve`, {});
  }
  rejectAddition(id: string, entryId: string, reason: string): Observable<AdditionDecision> {
    return this.http.post<AdditionDecision>(`${this.base}/${id}/roll-call/${entryId}/reject`, { reason });
  }
  /**
   * Gives a Student who lost their seat (not signed in by 2 hours after the class started)
   * their seat back: the class's lead or an admin. When the approved sessions have no room
   * it waits for an approver (status `pending`).
   */
  reinstate(id: string, entryId: string): Observable<ReinstateResult> {
    return this.http.post<ReinstateResult>(`${this.base}/${id}/roll-call/${entryId}/reinstate`, {});
  }
  /** Marks the entry removed; it stays on record. */
  remove(id: string, entryId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}/roll-call/${entryId}`);
  }
}
