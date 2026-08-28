import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { environment } from '@env/environment';
import { AuthService, RegistrationPrefill, RegistrationSuggestions } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';

interface Nation {
  id: string;
  name: string;
  iso_alpha3: string;
  flag_emoji: string;
}

interface Qualification {
  id: string;
  qsp_code: string;
  title: string;
}

/** Mirrors RegistrationRequestIn in the API's schemas.py. */
interface RegistrationForm {
  rank: string;
  service_branch: string;
  unit: string;
  callsign: string;
  nation_id: string | null;
  timezone: string;
  requested_qualification_id: string | null;
  requested_cohort: string;
  justification: string;
}

/**
 * Request an account.
 *
 * Reached when an AD-authenticated identity has no TrueNorth user row. Identity
 * itself is never editable here — it comes from the directory, and letting
 * someone retype their own name would only desynchronise the two. The form
 * collects what AD does not hold.
 */
@Component({
  selector: 'tn-register',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
  ],
  template: `
    <div class="register-shell">
      <mat-card class="register-card">
        <mat-card-header>
          <mat-card-title>Request access to TrueNorth Range</mat-card-title>
          <mat-card-subtitle>
            You are signed in as
            <strong>{{ prefill()?.display_name || prefill()?.email }}</strong
            >. An instructor reviews every request before an account is created.
          </mat-card-subtitle>
        </mat-card-header>

        <mat-card-content>
          @if (loading()) {
            <div class="centred"><mat-spinner diameter="36" /></div>
          } @else {
            @if (!suggestions()?.may_register) {
              <div class="notice notice--error">
                <mat-icon>block</mat-icon>
                <div>
                  Your directory account is not in a group permitted to register. Contact your
                  training authority if you believe this is wrong.
                </div>
              </div>
            }

            <section class="from-ad">
              <h3>
                From Active Directory
                <mat-icon
                  class="hint"
                  matTooltip="Supplied by the directory and not editable here.">
                  info_outline
                </mat-icon>
              </h3>
              <div class="ad-grid">
                <div><span>Name</span><strong>{{ prefill()?.display_name }}</strong></div>
                <div><span>Email</span><strong>{{ prefill()?.email }}</strong></div>
                @if (suggestions()?.matched_groups?.length) {
                  <div class="wide">
                    <span>Groups</span>
                    <strong>{{ suggestions()?.matched_groups?.join(', ') }}</strong>
                  </div>
                }
              </div>
            </section>

            <section class="fields">
              <h3>About you</h3>
              <div class="grid">
                <mat-form-field appearance="outline">
                  <mat-label>Rank</mat-label>
                  <input matInput [(ngModel)]="form.rank" name="rank" />
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Service / branch</mat-label>
                  <input matInput [(ngModel)]="form.service_branch" name="service_branch" />
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Unit</mat-label>
                  <input matInput [(ngModel)]="form.unit" name="unit" />
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Callsign</mat-label>
                  <input matInput [(ngModel)]="form.callsign" name="callsign" />
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Nation</mat-label>
                  <mat-select [(ngModel)]="form.nation_id" name="nation_id">
                    @for (n of nations(); track n.id) {
                      <mat-option [value]="n.id">{{ n.flag_emoji }} {{ n.name }}</mat-option>
                    }
                  </mat-select>
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Time zone</mat-label>
                  <input matInput [(ngModel)]="form.timezone" name="timezone" />
                </mat-form-field>
              </div>
            </section>

            <section class="fields">
              <h3>What are you joining?</h3>
              <div class="grid">
                <mat-form-field appearance="outline">
                  <mat-label>Qualification</mat-label>
                  <mat-select
                    [(ngModel)]="form.requested_qualification_id"
                    name="requested_qualification_id">
                    @for (q of qualifications(); track q.id) {
                      <mat-option [value]="q.id">{{ q.qsp_code }} — {{ q.title }}</mat-option>
                    }
                  </mat-select>
                </mat-form-field>

                <mat-form-field appearance="outline">
                  <mat-label>Cohort / serial</mat-label>
                  <input matInput [(ngModel)]="form.requested_cohort" name="requested_cohort" />
                </mat-form-field>
              </div>

              <mat-form-field appearance="outline" class="full">
                <mat-label>Anything the approver should know (optional)</mat-label>
                <textarea matInput rows="3" [(ngModel)]="form.justification" name="justification">
                </textarea>
              </mat-form-field>
            </section>
          }
        </mat-card-content>

        <mat-card-actions align="end">
          <button mat-button type="button" (click)="signOut()">Sign out</button>
          <button
            mat-flat-button
            color="primary"
            [disabled]="submitting() || loading() || !suggestions()?.may_register"
            (click)="submit()">
            @if (submitting()) {
              <mat-spinner diameter="18" />
            } @else {
              Submit request
            }
          </button>
        </mat-card-actions>
      </mat-card>
    </div>
  `,
  styles: [
    `
      .register-shell {
        display: flex;
        justify-content: center;
        padding: 2rem 1rem;
      }
      .register-card {
        width: min(880px, 100%);
      }
      mat-card-content {
        display: block;
        padding-top: 1rem;
      }
      .centred {
        display: flex;
        justify-content: center;
        padding: 2rem;
      }
      h3 {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        margin: 1.25rem 0 0.5rem;
        font-size: 0.95rem;
        font-weight: 600;
      }
      .hint {
        font-size: 1rem;
        width: 1rem;
        height: 1rem;
        opacity: 0.6;
      }
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 0.5rem 1rem;
      }
      .full {
        width: 100%;
      }
      .from-ad .ad-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 0.75rem 1rem;
        padding: 0.75rem 1rem;
        border-radius: 8px;
        background: var(--tn-surface-2, rgba(127, 127, 127, 0.08));
      }
      .ad-grid > div {
        display: flex;
        flex-direction: column;
      }
      .ad-grid .wide {
        grid-column: 1 / -1;
      }
      .ad-grid span {
        font-size: 0.75rem;
        opacity: 0.7;
      }
      .notice {
        display: flex;
        gap: 0.75rem;
        align-items: flex-start;
        padding: 0.75rem 1rem;
        border-radius: 8px;
        margin-bottom: 1rem;
      }
      .notice--error {
        background: rgba(211, 47, 47, 0.12);
      }
    `,
  ],
})
export class RegisterComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);

  loading = signal(true);
  submitting = signal(false);
  prefill = signal<RegistrationPrefill | null>(null);
  suggestions = signal<RegistrationSuggestions | null>(null);
  nations = signal<Nation[]>([]);
  qualifications = signal<Qualification[]>([]);

  form: RegistrationForm = {
    rank: '',
    service_branch: '',
    unit: '',
    callsign: '',
    nation_id: null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    requested_qualification_id: null,
    requested_cohort: '',
    justification: '',
  };

  async ngOnInit(): Promise<void> {
    const me = await this.auth.bootstrap(true);
    this.prefill.set(me?.prefill ?? null);
    this.suggestions.set(me?.suggestions ?? null);

    // Reference data is best-effort: an empty nation list should not stop
    // someone registering.
    this.http.get<Nation[]>(`${environment.apiUrl}/directory/nations`).subscribe({
      next: (rows) => this.nations.set(rows ?? []),
      error: () => this.nations.set([]),
    });
    this.http.get<Qualification[]>(`${environment.apiUrl}/qsp/qualifications`).subscribe({
      next: (rows) => this.qualifications.set(rows ?? []),
      error: () => this.qualifications.set([]),
    });

    this.loading.set(false);
  }

  submit(): void {
    this.submitting.set(true);
    this.http.post(`${environment.apiUrl}/registration`, this.form).subscribe({
      next: async () => {
        await this.auth.bootstrap(true);
        this.submitting.set(false);
        void this.router.navigate(['/registration-pending']);
      },
      error: (err) => {
        this.submitting.set(false);
        this.notify.error(err?.error?.detail ?? 'Could not submit your request.');
      },
    });
  }

  signOut(): void {
    this.auth.logout();
  }
}
