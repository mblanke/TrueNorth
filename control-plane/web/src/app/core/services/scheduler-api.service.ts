import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type Booking = S['EventOut'];
export type BookingIn = S['EventIn'];
export type CapacityCheck = S['CapacityCheck'];
export type CapacityResult = S['CapacityResult'];
export type Timeline = S['TimelineOut'];
export type OvercapacityPolicy = S['PolicyOut']['overcapacity'];
export type FeedIssued = S['FeedTokenIssued'];
export type FeedStatus = S['FeedTokenStatus'];
export type MySession = S['MySessionOut'];
export type CourseOption = S['CourseListOut'];
export type ApprovalItem = S['ApprovalItemOut'];
export type Fit = S['FitOut'];
export type SchedulerSettings = S['SchedulerSettingsOut'];

/**
 * The scheduler's HTTP contract (docs/adr/0004-scheduler-module.md), typed from the
 * generated OpenAPI schema. Same base-URL convention as ApiService.
 */
@Injectable({ providedIn: 'root' })
export class SchedulerApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/schedule`;

  // ── Bookings ─────────────────────────────────────────────
  /** Bookings overlapping [start, end): the calendar's week. */
  list(start: Date, end: Date, limit = 200): Observable<S['EventListOut']> {
    const params = new HttpParams().set('start', start.toISOString()).set('end', end.toISOString()).set('limit', limit);
    return this.http.get<S['EventListOut']>(`${this.base}/events`, { params });
  }
  create(body: BookingIn): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events`, body);
  }
  update(id: string, body: BookingIn): Observable<Booking> {
    return this.http.put<Booking>(`${this.base}/events/${id}`, body);
  }
  /** draft -> scheduled, checked for conflicts and capacity first. */
  schedule(id: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/schedule`, {});
  }
  cancel(id: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/cancel`, {});
  }

  // ── Class-session approval (ADR 0014 §14-16) ─────────────
  /** The tenant's class sessions awaiting approval, with fit and next free slot (schedule:approve). */
  approvals(): Observable<S['ApprovalQueueOut']> {
    return this.http.get<S['ApprovalQueueOut']>(`${this.base}/approvals`);
  }
  approve(id: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/approve`, {});
  }
  reject(id: string, reason: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/reject`, { reason });
  }
  propose(id: string, start: Date, end: Date): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/propose`, {
      start_time: start.toISOString(), end_time: end.toISOString(),
    });
  }
  /** The session's instructor takes the proposed time: re-checked, moved and approved. */
  acceptProposal(id: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/proposal/accept`, {});
  }
  declineProposal(id: string): Observable<Booking> {
    return this.http.post<Booking>(`${this.base}/events/${id}/proposal/decline`, {});
  }
  nextFree(id: string): Observable<Fit> {
    return this.http.get<Fit>(`${this.base}/events/${id}/next-free`);
  }

  /** Your own upcoming sessions: ones you teach, or for a Student, your courses'. */
  mine(): Observable<MySession[]> {
    return this.http.get<MySession[]>(`${this.base}/mine`);
  }
  /** Courses a booking can be for (its class). */
  courses(): Observable<S['PaginatedResponse_CourseListOut_']> {
    const params = new HttpParams().set('limit', 200).set('include_retired', false);
    return this.http.get<S['PaginatedResponse_CourseListOut_']>(`${environment.apiUrl}/courses`, { params });
  }

  // ── Capacity ─────────────────────────────────────────────
  check(body: CapacityCheck): Observable<CapacityResult> {
    return this.http.post<CapacityResult>(`${this.base}/check`, body);
  }
  timeline(start: Date, days: number, resolutionMinutes: 15 | 30 | 60 = 15): Observable<Timeline> {
    const params = new HttpParams()
      .set('start', start.toISOString())
      .set('days', days)
      .set('resolution_minutes', resolutionMinutes);
    return this.http.get<Timeline>(`${this.base}/timeline`, { params });
  }

  // ── Site settings the pages say in words (#318) ──────────
  /** Provisioning lead, teardown grace, next-free horizon and site time zone (schedule:read). */
  settings(): Observable<SchedulerSettings> {
    return this.http.get<SchedulerSettings>(`${this.base}/settings`);
  }

  // ── Over-capacity policy (setting it is admin only) ──────
  getPolicy(): Observable<S['PolicyOut']> {
    return this.http.get<S['PolicyOut']>(`${this.base}/policy`);
  }
  setPolicy(overcapacity: OvercapacityPolicy): Observable<S['PolicyOut']> {
    return this.http.put<S['PolicyOut']>(`${this.base}/policy`, { overcapacity });
  }

  // ── Calendar feed ────────────────────────────────────────
  feedStatus(): Observable<FeedStatus> {
    return this.http.get<FeedStatus>(`${this.base}/feed-token`);
  }
  /** Creates or regenerates the link; the previous one stops working. Shown once. */
  issueFeed(): Observable<FeedIssued> {
    return this.http.post<FeedIssued>(`${this.base}/feed-token`, {});
  }
  revokeFeed(): Observable<void> {
    return this.http.delete<void>(`${this.base}/feed-token`);
  }
}
