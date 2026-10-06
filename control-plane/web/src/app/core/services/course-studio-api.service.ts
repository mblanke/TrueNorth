import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type CourseRelease = S['CourseReleaseOut'];
export type CoursePublication = S['CoursePublicationOut'];
export type LabSession = S['LabSessionOut'];
export type LabConsole = S['ConsoleOut'];
export type LearningPlatform = S['ExternalPlatformOut'];

/**
 * Course Studio: ARC² releases (upload, accept, download parts), their publication to
 * Moodle, and students' lab sessions. Lab calls go one of two ways: as the signed-in
 * user (`/lab-sessions`), or with the session's own token after a Moodle launch
 * (`/lab-access`, header X-Lab-Token), which opens that one session and nothing else.
 */
@Injectable({ providedIn: 'root' })
export class CourseStudioApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  // ── Releases ─────────────────────────────────────────────
  releases(courseId?: string): Observable<CourseRelease[]> {
    const query = courseId ? `?course_id=${encodeURIComponent(courseId)}` : '';
    return this.http.get<CourseRelease[]>(`${this.base}/course-releases${query}`);
  }
  upload(file: File): Observable<CourseRelease> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<CourseRelease>(`${this.base}/course-releases`, form);
  }
  accept(id: string, body: S['AcceptIn']): Observable<CourseRelease> {
    return this.http.post<CourseRelease>(`${this.base}/course-releases/${id}/accept`, body);
  }
  instructorBundleUrl(id: string): string {
    return `${this.base}/course-releases/${id}/instructor-bundle`;
  }
  instructorBundle(id: string): Observable<Blob> {
    return this.http.get(this.instructorBundleUrl(id), { responseType: 'blob' });
  }

  // ── Publication to Moodle ────────────────────────────────
  platforms(): Observable<LearningPlatform[]> {
    return this.http.get<LearningPlatform[]>(`${this.base}/integrations/platforms`);
  }
  publications(releaseId: string): Observable<CoursePublication[]> {
    return this.http.get<CoursePublication[]>(`${this.base}/course-releases/${releaseId}/publications`);
  }
  publish(releaseId: string, platformId: string): Observable<CoursePublication> {
    return this.http.post<CoursePublication>(`${this.base}/course-releases/${releaseId}/publications`, {
      platform_id: platformId,
    });
  }
  retryPublication(id: string): Observable<CoursePublication> {
    return this.http.post<CoursePublication>(`${this.base}/course-publications/${id}/retry`, {});
  }

  // ── Labs ─────────────────────────────────────────────────
  lab(id: string, token?: string): Observable<LabSession> {
    return token
      ? this.http.get<LabSession>(`${this.base}/lab-access/${id}`, { headers: this.labHeaders(token) })
      : this.http.get<LabSession>(`${this.base}/lab-sessions/${id}`);
  }
  labAction(id: string, action: 'heartbeat' | 'reset' | 'end', token?: string): Observable<LabSession> {
    return token
      ? this.http.post<LabSession>(`${this.base}/lab-access/${id}/${action}`, {}, { headers: this.labHeaders(token) })
      : this.http.post<LabSession>(`${this.base}/lab-sessions/${id}/${action}`, {});
  }
  labConsole(id: string, token?: string): Observable<LabConsole> {
    return token
      ? this.http.post<LabConsole>(`${this.base}/lab-access/${id}/console`, {}, { headers: this.labHeaders(token) })
      : this.http.post<LabConsole>(`${this.base}/lab-sessions/${id}/console`, {});
  }

  private labHeaders(token: string): HttpHeaders {
    return new HttpHeaders({ 'X-Lab-Token': token });
  }
}
