import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type Cmi5Structure = S['Cmi5StructureOut'];
export type Cmi5Au = S['Cmi5AuOut'];
export type Cmi5Content = S['Cmi5ContentOut'];
export type Cmi5Grade = S['Cmi5GradeOut'];
export type Cmi5Launch = S['Cmi5LaunchOut'];
export type Cmi5LaunchMode = 'Normal' | 'Browse' | 'Review';

/**
 * TrueNorth's cmi5 routes (docs/cmi5.md): a release's AUs, an AU's content and quiz
 * marking for the AU runtime, and TrueNorth's own launch (TrueNorth as the cmi5 LMS).
 * The AU's xAPI traffic does not go through here: it uses the launch's own endpoint and
 * session token (features/cmi5/cmi5-au.ts), never the Student's TrueNorth sign-in.
 */
@Injectable({ providedIn: 'root' })
export class Cmi5ApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  private au(releaseId: string, index: number): string {
    return `${this.base}/cmi5/releases/${encodeURIComponent(releaseId)}/aus/${index}`;
  }

  structure(releaseId: string): Observable<Cmi5Structure> {
    return this.http.get<Cmi5Structure>(`${this.base}/cmi5/releases/${encodeURIComponent(releaseId)}/structure`);
  }

  content(releaseId: string, index: number): Observable<Cmi5Content> {
    return this.http.get<Cmi5Content>(`${this.au(releaseId, index)}/content`);
  }

  grade(releaseId: string, index: number, answers: Record<string, string>): Observable<Cmi5Grade> {
    return this.http.post<Cmi5Grade>(`${this.au(releaseId, index)}/grade`, { answers });
  }

  launch(releaseId: string, index: number, launchMode: Cmi5LaunchMode | null = null): Observable<Cmi5Launch> {
    return this.http.post<Cmi5Launch>(`${this.au(releaseId, index)}/launch`, { launch_mode: launchMode });
  }
}
