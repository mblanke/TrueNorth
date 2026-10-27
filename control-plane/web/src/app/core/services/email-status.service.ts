import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002).
export type EmailStatus = S['EmailStatusOut'];
export type EmailTestResult = S['EmailTestOut'];

/** Where an operator sets the relay: the installer's README, "Email (SMTP)". */
export const EMAIL_DOCS_URL = 'https://github.com/mblanke/TrueNorth/blob/main/install/README.md#email-smtp';

/**
 * Whether the API sends email (GET /email/status, control-plane/api/app/email_status; #306),
 * shared by the admin banner and the Classes page's roll-call warning. Presentation only:
 * the API is what sends, or only logs, the mail.
 */
@Injectable({ providedIn: 'root' })
export class EmailStatusService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/email`;
  private inFlight = false;

  /** Null until the server answers. */
  readonly status = signal<EmailStatus | null>(null);
  /** True only once the server SAYS email is off; an unanswered check warns nobody. */
  readonly notConfigured = computed(() => this.status()?.configured === false);

  /** Ask once per session; `force` asks again. */
  load(force = false): void {
    if (!force && (this.status() || this.inFlight)) return;
    this.inFlight = true;
    this.http.get<EmailStatus>(`${this.base}/status`).subscribe({
      next: s => { this.inFlight = false; this.status.set(s); },
      error: () => { this.inFlight = false; },
    });
  }

  /** Platform administrator only: one message to their own address (429 within a minute). */
  sendTest(): Observable<EmailTestResult> {
    return this.http.post<EmailTestResult>(`${this.base}/test`, {});
  }
}
