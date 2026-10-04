import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type RegistrationRequest = S['RegistrationRequestOut'];
export type OnboardingState = S['OnboardingStateOut'];

/**
 * Account intake: self-registration, the instructor approval queue, and first-run
 * onboarding. Same base-URL convention as ApiService.
 */
@Injectable({ providedIn: 'root' })
export class RegistrationApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  // ── Self-registration ────────────────────────────────────
  submit(body: S['RegistrationRequestIn']): Observable<RegistrationRequest> {
    return this.http.post<RegistrationRequest>(`${this.base}/registration`, body);
  }
  withdrawMine(): Observable<void> {
    return this.http.delete<void>(`${this.base}/registration/mine`);
  }

  // ── Approval queue ───────────────────────────────────────
  /** `query` is appended verbatim (status, optional cohort). */
  listRequests(query: URLSearchParams): Observable<RegistrationRequest[]> {
    return this.http.get<RegistrationRequest[]>(`${this.base}/registration/requests?${query}`);
  }
  approve(id: string, body: S['RegistrationApproveIn']): Observable<RegistrationRequest> {
    return this.http.post<RegistrationRequest>(`${this.base}/registration/requests/${id}/approve`, body);
  }
  reject(id: string, body: S['RegistrationRejectIn']): Observable<RegistrationRequest> {
    return this.http.post<RegistrationRequest>(`${this.base}/registration/requests/${id}/reject`, body);
  }
  bulkApprove(body: S['RegistrationBulkApproveIn']): Observable<S['RegistrationBulkApproveOut']> {
    return this.http.post<S['RegistrationBulkApproveOut']>(
      `${this.base}/registration/requests/bulk-approve`, body,
    );
  }

  // ── First-run onboarding ─────────────────────────────────
  onboardingState(): Observable<OnboardingState> {
    return this.http.get<OnboardingState>(`${this.base}/onboarding/state`);
  }
  saveOnboardingProfile(body: S['OnboardingProfileIn']): Observable<OnboardingState> {
    return this.http.post<OnboardingState>(`${this.base}/onboarding/profile`, body);
  }
  selectOnboardingPath(body: S['OnboardingPathIn']): Observable<OnboardingState> {
    return this.http.post<OnboardingState>(`${this.base}/onboarding/select-path`, body);
  }
  completeOnboarding(): Observable<OnboardingState> {
    return this.http.post<OnboardingState>(`${this.base}/onboarding/complete`, {});
  }
  skipOnboarding(): Observable<OnboardingState> {
    return this.http.post<OnboardingState>(`${this.base}/onboarding/skip`, {});
  }
}
