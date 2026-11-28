// Author: Marc-Andre Blanke
// Created: 2026-11-27T00:00:00Z
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type StudentList = S['StudentListOut'];
export type StudentRow = S['StudentRowOut'];
export type StudentRecord = S['StudentRecordOut'];
export type StudentClass = S['StudentClassOut'];

/** The page's filters; an empty string is "not set". */
export interface StudentFilter {
  unit: string;
  q: string;
}

/**
 * The Students page (readiness:read + learning_record:read: instructors and admins). The API
 * keeps it to the caller's own tenant and only reads.
 */
@Injectable({ providedIn: 'root' })
export class StudentProgressApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/students`;

  list(f: StudentFilter): Observable<StudentList> {
    let p = new HttpParams();
    if (f.unit.trim()) p = p.set('unit', f.unit.trim());
    if (f.q.trim()) p = p.set('q', f.q.trim());
    return this.http.get<StudentList>(this.base, { params: p });
  }

  record(userId: string): Observable<StudentRecord> {
    return this.http.get<StudentRecord>(`${this.base}/${encodeURIComponent(userId)}`);
  }
}
