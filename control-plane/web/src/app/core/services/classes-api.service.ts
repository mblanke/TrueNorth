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
export type ClassRanges = S['ClassRangesOut'];
export type ClassRange = S['ClassRangeRowOut'];
export type ClassRangeStatus = S['ClassRangeStatus'];
export type ClassSessionMode = S['ClassSessionModeOut'];
export type RangeMode = S['RangeModeOut'];
export type CourseRangeMode = S['CourseRangeModeOut'];
export type SessionTeams = S['SessionTeamsOut'];

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
  /** Marks the entry removed; it stays on record. */
  remove(id: string, entryId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}/roll-call/${entryId}`);
  }

  // ── Ranges, session modes and teams (ADR 0014 §9-11; #282) ──
  /** Its sessions with their mode, and its ranges with their status. No credentials. */
  ranges(id: string): Observable<ClassRanges> {
    return this.http.get<ClassRanges>(`${this.base}/${id}/ranges`);
  }
  /** Build a range whose build failed again (its lead or an admin). */
  retryRange(id: string, classRangeId: string): Observable<ClassRange> {
    return this.http.post<ClassRange>(`${this.base}/${id}/ranges/${classRangeId}/retry`, {});
  }
  courseRangeMode(courseId: string): Observable<CourseRangeMode> {
    return this.http.get<CourseRangeMode>(`${this.base}/course-range-modes/${courseId}`);
  }
  setCourseRangeMode(courseId: string, mode: RangeMode): Observable<CourseRangeMode> {
    return this.http.put<CourseRangeMode>(`${this.base}/course-range-modes/${courseId}`, { mode });
  }
  sessionTeams(id: string, eventId: string): Observable<SessionTeams> {
    return this.http.get<SessionTeams>(`${this.base}/${id}/sessions/${eventId}/teams`);
  }
  /** Balanced teams from the roll call; splitting again places everyone again. */
  splitTeams(id: string, eventId: string, teamCount: number): Observable<SessionTeams> {
    return this.http.post<SessionTeams>(`${this.base}/${id}/sessions/${eventId}/teams`, { team_count: teamCount });
  }
  moveTeamMember(id: string, eventId: string, userId: string, teamId: string): Observable<SessionTeams> {
    return this.http.put<SessionTeams>(`${this.base}/${id}/sessions/${eventId}/teams/members/${userId}`, { team_id: teamId });
  }
}
