import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type ExerciseTemplate = S['ExerciseTemplateOut'];
export type ExerciseTemplateRow = S['ExerciseTemplateListOut'];
export type ExerciseTemplateInput = S['ExerciseTemplateIn'];
export type ExerciseTemplateChanges = S['ExerciseTemplateUpdate'];
export type TemplateTeam = S['ExerciseTeamSpec'];
export type TemplateRole = S['ExerciseRoleSpec'];
export type TemplateVm = S['ExerciseVmSpec'];
export type TemplateScalingRule = S['ExerciseScalingRuleSpec'];
export type TemplateObjective = S['ExerciseObjectiveSpec'];
export type TemplateSerial = S['ExerciseMeslSpec'];
export type ExerciseFromTemplate = S['ExerciseFromTemplateOut'];
export type ExerciseMadeFromTemplate = S['ExerciseMadeFromTemplateOut'];

export const DELIVERY_METHODS: readonly NonNullable<TemplateSerial['delivery_method']>[] =
  ['cyber', 'white_cell', 'email', 'radio', 'physical', 'opfor'];
export const SCALING_KINDS: readonly NonNullable<TemplateScalingRule['kind']>[] = ['per_segment', 'per_participants', 'fixed'];
export const TEAM_SIDES: readonly NonNullable<TemplateTeam['side']>[] = ['blue', 'red', 'white', 'green', 'other'];

/**
 * Exercise templates (ADR 0017): the reusable part of an exercise, and creating an exercise
 * from one. Staff only (exercise:create). Every call goes through this client so the auth
 * interceptor and base URL apply.
 */
@Injectable({ providedIn: 'root' })
export class ExerciseTemplatesApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/exercise-templates`;

  list(): Observable<ExerciseTemplateRow[]> {
    return this.http.get<ExerciseTemplateRow[]>(this.base);
  }
  get(id: string): Observable<ExerciseTemplate> {
    return this.http.get<ExerciseTemplate>(`${this.base}/${id}`);
  }
  create(body: ExerciseTemplateInput): Observable<ExerciseTemplate> {
    return this.http.post<ExerciseTemplate>(this.base, body);
  }
  update(id: string, body: ExerciseTemplateChanges): Observable<ExerciseTemplate> {
    return this.http.put<ExerciseTemplate>(`${this.base}/${id}`, body);
  }
  remove(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }
  /** Copies the template's objectives and MESL into a new exercise on `rangeId`. */
  createExercise(id: string, body: { name: string; range_id: string; max_score?: number | null }):
    Observable<ExerciseFromTemplate> {
    return this.http.post<ExerciseFromTemplate>(`${this.base}/${id}/exercises`, body);
  }
  exercises(id: string): Observable<ExerciseMadeFromTemplate[]> {
    return this.http.get<ExerciseMadeFromTemplate[]>(`${this.base}/${id}/exercises`);
  }
  /** "Save as template" on an existing exercise; the exercise is not changed. */
  saveExercise(exerciseId: string, body: { name: string; description?: string }): Observable<ExerciseTemplate> {
    return this.http.post<ExerciseTemplate>(`${this.base}/from-exercise/${exerciseId}`, body);
  }
}
