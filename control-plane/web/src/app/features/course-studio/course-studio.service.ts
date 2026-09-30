import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import { RunDetail, RunList } from '@core/arc2/studio';

/** The ARC² Course Studio API (control-plane/api/app/routers/arc2_studio.py). */
@Injectable({ providedIn: 'root' })
export class CourseStudioService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/arc2`;

  list(): Observable<RunList> {
    return this.http.get<RunList>(`${this.base}/runs`);
  }
  get(slug: string): Observable<RunDetail> {
    return this.http.get<RunDetail>(`${this.base}/runs/${encodeURIComponent(slug)}`);
  }
  /** Send: create the project and start stage 1. */
  create(name: string, request: string): Observable<RunDetail> {
    return this.http.post<RunDetail>(`${this.base}/runs`, { name, request });
  }
  reply(slug: string, action: 'accept' | 'feedback', text?: string): Observable<RunDetail> {
    return this.http.post<RunDetail>(`${this.base}/runs/${encodeURIComponent(slug)}/reply`, { action, text });
  }
  file(slug: string, path: string): Observable<{ path: string; text: string; instructor_only: boolean }> {
    return this.http.get<{ path: string; text: string; instructor_only: boolean }>(
      `${this.base}/runs/${encodeURIComponent(slug)}/file`, { params: { path } });
  }
  packageZip(slug: string): Observable<Blob> {
    return this.http.get(`${this.base}/runs/${encodeURIComponent(slug)}/package.zip`, { responseType: 'blob' });
  }
}
