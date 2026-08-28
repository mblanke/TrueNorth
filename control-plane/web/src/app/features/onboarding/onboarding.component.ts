import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { MatStepperModule } from '@angular/material/stepper';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatListModule } from '@angular/material/list';
import { environment } from '@env/environment';
import { AuthService } from '@core/services/auth.service';
import { TourService } from '../../shared/tour/tour.service';
import { NotificationService } from '@core/services/notification.service';

interface OnboardingState {
  state: string;
  steps_done: string[];
  missing_profile_fields: string[];
  qualification_id: string | null;
  learning_path_id: string | null;
  enrolled_course_count: number;
  onboarded_at: string | null;
}

interface Nation {
  id: string;
  name: string;
  flag_emoji: string;
}

interface Qualification {
  id: string;
  qsp_code: string;
  title: string;
}

/** Mirrors OnboardingProfileIn in the API's schemas.py. */
interface ProfileForm {
  rank: string;
  service_branch: string;
  unit: string;
  callsign: string;
  nation_id: string | null;
  timezone: string;
}

/**
 * First run, immediately after an instructor approves the account.
 *
 * Confirm rather than re-ask: registration already collected the profile, so
 * this step shows it back and lets it be corrected. Making a newly approved
 * trainee retype what they typed last week would be its own small insult.
 */
@Component({
  selector: 'tn-onboarding',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatStepperModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatListModule,
  ],
  template: `
    <div class="shell">
      <mat-card class="card">
        <mat-card-header>
          <mat-card-title>Welcome to TrueNorth Range</mat-card-title>
          <mat-card-subtitle>Three short steps and you are on the range.</mat-card-subtitle>
        </mat-card-header>

        <mat-card-content>
          @if (loading()) {
            <div class="centred"><mat-spinner diameter="36" /></div>
          } @else {
            <mat-stepper orientation="vertical" [linear]="false" #stepper>
              <!-- 1. Profile -->
              <mat-step label="Confirm your details">
                <p class="muted">
                  Carried over from your access request. Correct anything that has changed.
                </p>
                <div class="grid">
                  <mat-form-field appearance="outline">
                    <mat-label>Rank</mat-label>
                    <input matInput [(ngModel)]="profile.rank" name="rank" />
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Service / branch</mat-label>
                    <input matInput [(ngModel)]="profile.service_branch" name="service_branch" />
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Unit</mat-label>
                    <input matInput [(ngModel)]="profile.unit" name="unit" />
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Callsign</mat-label>
                    <input matInput [(ngModel)]="profile.callsign" name="callsign" />
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Nation</mat-label>
                    <mat-select [(ngModel)]="profile.nation_id" name="nation_id">
                      @for (n of nations(); track n.id) {
                        <mat-option [value]="n.id">{{ n.flag_emoji }} {{ n.name }}</mat-option>
                      }
                    </mat-select>
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Time zone</mat-label>
                    <input matInput [(ngModel)]="profile.timezone" name="timezone" />
                  </mat-form-field>
                </div>
                <div class="actions">
                  <button mat-flat-button color="primary" [disabled]="busy()" (click)="saveProfile(stepper)">
                    Save and continue
                  </button>
                </div>
              </mat-step>

              <!-- 2. Path -->
              <mat-step label="Your training path">
                <p class="muted">
                  This is the developmental progression you have been enrolled on. Your instructor
                  can change it later.
                </p>
                <mat-form-field appearance="outline" class="full">
                  <mat-label>Qualification</mat-label>
                  <mat-select [(ngModel)]="selectedQualification" name="qual">
                    @for (q of qualifications(); track q.id) {
                      <mat-option [value]="q.id">{{ q.qsp_code }} — {{ q.title }}</mat-option>
                    }
                  </mat-select>
                </mat-form-field>
                <div class="actions">
                  <button mat-button matStepperPrevious>Back</button>
                  <button mat-flat-button color="primary" [disabled]="busy()" (click)="savePath(stepper)">
                    Enrol me
                  </button>
                </div>
              </mat-step>

              <!-- 3. Tour -->
              <mat-step label="Where things are">
                <mat-list>
                  <mat-list-item>
                    <mat-icon matListItemIcon>school</mat-icon>
                    <div matListItemTitle>Learning</div>
                    <div matListItemLine>Your qualification, courses and progress.</div>
                  </mat-list-item>
                  <mat-list-item>
                    <mat-icon matListItemIcon>military_tech</mat-icon>
                    <div matListItemTitle>Exercises</div>
                    <div matListItemLine>Live range work, and the Ops Center while one runs.</div>
                  </mat-list-item>
                  <mat-list-item>
                    <mat-icon matListItemIcon>insights</mat-icon>
                    <div matListItemTitle>Scoring &amp; AAR</div>
                    <div matListItemLine>How you did, and what to do about it.</div>
                  </mat-list-item>
                </mat-list>
                @if (state()?.enrolled_course_count) {
                  <p class="enrolled">
                    <mat-icon>check_circle</mat-icon>
                    Enrolled on {{ state()?.enrolled_course_count }} course(s).
                  </p>
                }
                <div class="actions">
                  <button mat-button matStepperPrevious>Back</button>
                  <button mat-stroked-button (click)="showMeAround()">Show me around</button>
                  <button mat-flat-button color="primary" [disabled]="busy()" (click)="finish()">
                    Take me to my dashboard
                  </button>
                </div>
              </mat-step>
            </mat-stepper>
          }
        </mat-card-content>

        <mat-card-actions align="end">
          <button mat-button (click)="skip()">Skip for now</button>
        </mat-card-actions>
      </mat-card>
    </div>
  `,
  styles: [
    `
      .shell {
        display: flex;
        justify-content: center;
        padding: 2rem 1rem;
      }
      .card {
        width: min(820px, 100%);
      }
      .centred {
        display: flex;
        justify-content: center;
        padding: 2rem;
      }
      .muted {
        opacity: 0.75;
      }
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 0.5rem 1rem;
      }
      .full {
        width: 100%;
      }
      .actions {
        display: flex;
        gap: 0.5rem;
        margin-top: 0.75rem;
      }
      .enrolled {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        opacity: 0.85;
      }
    `,
  ],
})
export class OnboardingComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly tour = inject(TourService);

  loading = signal(true);
  busy = signal(false);
  state = signal<OnboardingState | null>(null);
  nations = signal<Nation[]>([]);
  qualifications = signal<Qualification[]>([]);
  selectedQualification: string | null = null;

  profile: ProfileForm = {
    rank: '',
    service_branch: '',
    unit: '',
    callsign: '',
    nation_id: null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  };

  ngOnInit(): void {
    this.http.get<OnboardingState>(`${environment.apiUrl}/onboarding/state`).subscribe({
      next: (s) => {
        this.state.set(s);
        this.selectedQualification = s.qualification_id;
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });

    this.http.get<Nation[]>(`${environment.apiUrl}/directory/nations`).subscribe({
      next: (rows) => this.nations.set(rows ?? []),
      error: () => this.nations.set([]),
    });
    this.http.get<Qualification[]>(`${environment.apiUrl}/qsp/qualifications`).subscribe({
      next: (rows) => this.qualifications.set(rows ?? []),
      error: () => this.qualifications.set([]),
    });
  }

  saveProfile(stepper: { next: () => void }): void {
    this.busy.set(true);
    this.http.post<OnboardingState>(`${environment.apiUrl}/onboarding/profile`, this.profile).subscribe({
      next: (s) => {
        this.state.set(s);
        this.busy.set(false);
        stepper.next();
      },
      error: () => {
        this.busy.set(false);
        this.notify.error('Could not save your details.');
      },
    });
  }

  savePath(stepper: { next: () => void }): void {
    if (!this.selectedQualification) {
      stepper.next();
      return;
    }
    this.busy.set(true);
    this.http
      .post<OnboardingState>(`${environment.apiUrl}/onboarding/select-path`, {
        qualification_id: this.selectedQualification,
      })
      .subscribe({
        next: (s) => {
          this.state.set(s);
          this.busy.set(false);
          stepper.next();
        },
        error: () => {
          this.busy.set(false);
          this.notify.error('Could not enrol you on that qualification.');
        },
      });
  }

  /**
   * Walk the sidebar, then land on the dashboard.
   *
   * The tour highlights the real navigation rather than a picture of it, so it
   * cannot drift out of date the way a screenshot would. Steps whose anchor is
   * missing are skipped, so a trainee who cannot see Authoring simply gets a
   * shorter tour.
   */
  showMeAround(): void {
    this.busy.set(true);
    this.http.post(`${environment.apiUrl}/onboarding/complete`, {}).subscribe({
      next: async () => {
        await this.auth.bootstrap(true);
        this.busy.set(false);
        await this.router.navigate(['/dashboard']);
        // One frame, so the routed view and the nav links exist to anchor to.
        requestAnimationFrame(() =>
          this.tour.start([
            {
              anchor: '/learning',
              title: 'Learning',
              body: 'Your qualification, your courses, and how far through them you are.',
            },
            {
              anchor: '/exercises',
              title: 'Exercises',
              body: 'Live range work. While one is running, the Ops Center is where you work.',
            },
            {
              anchor: '/scoring',
              title: 'Scoring and AAR',
              body: 'How you did, and what to do about it next time.',
            },
            {
              anchor: '/dashboard',
              title: 'Dashboard',
              body: 'Where you land. Anything waiting for you shows up here first.',
            },
          ]),
        );
      },
      error: () => {
        this.busy.set(false);
        this.notify.error('Could not complete onboarding.');
      },
    });
  }

  finish(): void {
    this.busy.set(true);
    this.http.post(`${environment.apiUrl}/onboarding/complete`, {}).subscribe({
      next: async () => {
        await this.auth.bootstrap(true);
        this.busy.set(false);
        void this.router.navigate(['/dashboard']);
      },
      error: () => {
        this.busy.set(false);
        this.notify.error('Could not complete onboarding.');
      },
    });
  }

  skip(): void {
    this.http.post(`${environment.apiUrl}/onboarding/skip`, {}).subscribe({
      next: async () => {
        await this.auth.bootstrap(true);
        void this.router.navigate(['/dashboard']);
      },
      error: () => this.notify.error('Could not skip onboarding.'),
    });
  }
}
