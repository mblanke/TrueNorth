import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type TeamSummary = S['TeamFullOut'];
export type TeamMember = S['TeamMemberOut'];
export type TeamMemberAdded = S['TeamMemberAddedOut'];
export type DirectoryUser = S['UserFullOut'];
export type CourseSummary = S['CourseListOut'];
export type CourseDetail = S['CourseOut'];
export type CourseEnrollment = S['EnrollmentOut'];
export type ModuleProgress = S['ModuleProgressOut'];
export type MoodleResultsStatus = S['MoodleResultsStatusOut'];
export type MoodleResultsPull = S['MoodleResultsPullOut'];
export interface CoursePage {
  items: CourseSummary[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * The R0-B operator records: team membership, a Student's course progress and
 * completion, and Moodle results. Typed against the generated contract.
 */
@Injectable({ providedIn: 'root' })
export class OperatorRecordsApiService {
  private http = inject(HttpClient);
  private base = environment.apiUrl;

  // ── Teams ──
  listTeams(): Observable<TeamSummary[]> {
    return this.http.get<TeamSummary[]>(`${this.base}/teams`);
  }
  listTeamMembers(teamId: string): Observable<TeamMember[]> {
    return this.http.get<TeamMember[]>(`${this.base}/teams/${teamId}/members`);
  }
  addTeamMember(teamId: string, userId: string, role = 'member'): Observable<TeamMemberAdded> {
    const params = new HttpParams().set('user_id', userId).set('role', role);
    return this.http.post<TeamMemberAdded>(`${this.base}/teams/${teamId}/members`, null, { params });
  }
  removeTeamMember(teamId: string, userId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/teams/${teamId}/members/${userId}`);
  }
  listUsers(): Observable<DirectoryUser[]> {
    return this.http.get<DirectoryUser[]>(`${this.base}/users`);
  }

  // ── Course records ──
  listCourses(limit = 200): Observable<CoursePage> {
    return this.http.get<CoursePage>(`${this.base}/courses`, { params: new HttpParams().set('limit', limit) });
  }
  getCourse(courseId: string): Observable<CourseDetail> {
    return this.http.get<CourseDetail>(`${this.base}/courses/${courseId}`);
  }
  listEnrollments(courseId: string): Observable<CourseEnrollment[]> {
    return this.http.get<CourseEnrollment[]>(`${this.base}/courses/${courseId}/enrollments`);
  }
  getProgress(courseId: string, userId: string): Observable<ModuleProgress[]> {
    return this.http.get<ModuleProgress[]>(`${this.base}/courses/${courseId}/progress/${userId}`);
  }
  markComplete(courseId: string, userId: string): Observable<CourseEnrollment> {
    return this.http.post<CourseEnrollment>(`${this.base}/courses/${courseId}/complete/${userId}`, {});
  }

  // ── Moodle results ──
  getMoodleResults(platformId: string): Observable<MoodleResultsStatus> {
    return this.http.get<MoodleResultsStatus>(`${this.base}/integrations/platforms/${platformId}/moodle-results`);
  }
  pullMoodleResults(platformId: string, reset = false): Observable<MoodleResultsPull> {
    return this.http.post<MoodleResultsPull>(
      `${this.base}/integrations/platforms/${platformId}/moodle-results/pull`, { reset },
    );
  }
}
