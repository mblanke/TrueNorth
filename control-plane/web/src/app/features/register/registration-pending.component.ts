import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { environment } from '@env/environment';
import { AuthService } from '@core/services/auth.service';

/**
 * The waiting room between submitting a request and an instructor deciding.
 *
 * Polls rather than holding a socket open: this page may sit for hours or days,
 * and a poll every 30s is cheaper than a connection per waiting applicant.
 */
@Component({
  selector: 'tn-registration-pending',
  standalone: true,
  imports: [CommonModule, MatCardModule, MatButtonModule, MatIconModule, MatProgressSpinnerModule],
  template: `
    <div class="shell">
      <mat-card class="card">
        @if (rejected()) {
          <mat-icon class="glyph glyph--rejected">cancel</mat-icon>
          <h2>Your request was declined</h2>
          @if (reason()) {
            <p class="reason">“{{ reason() }}”</p>
          }
          <p class="muted">
            You can submit a new request if your circumstances have changed, or speak to your
            training authority.
          </p>
          <div class="actions">
            <button mat-flat-button color="primary" (click)="resubmit()">Submit a new request</button>
            <button mat-button (click)="signOut()">Sign out</button>
          </div>
        } @else {
          <mat-icon class="glyph">hourglass_top</mat-icon>
          <h2>Waiting for approval</h2>
          <p class="muted">
            Your request has been sent to an instructor. This page updates on its own — you can
            leave it open, or sign out and come back later.
          </p>
          <div class="polling">
            <mat-spinner diameter="16" />
            <span>Checking every 30 seconds</span>
          </div>
          <div class="actions">
            <button mat-button (click)="checkNow()">Check now</button>
            <button mat-button (click)="withdraw()">Withdraw request</button>
            <button mat-button (click)="signOut()">Sign out</button>
          </div>
        }
      </mat-card>
    </div>
  `,
  styles: [
    `
      .shell {
        display: flex;
        justify-content: center;
        padding: 3rem 1rem;
      }
      .card {
        width: min(560px, 100%);
        text-align: center;
        padding: 2rem;
      }
      .glyph {
        font-size: 3rem;
        width: 3rem;
        height: 3rem;
        opacity: 0.7;
      }
      .glyph--rejected {
        color: var(--tn-error, #d32f2f);
      }
      h2 {
        margin: 0.75rem 0 0.5rem;
      }
      .muted {
        opacity: 0.75;
        margin: 0 auto 1.25rem;
        max-width: 44ch;
      }
      .reason {
        font-style: italic;
        margin: 0.5rem 0 1rem;
      }
      .polling {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
        font-size: 0.8rem;
        opacity: 0.7;
        margin-bottom: 1.25rem;
      }
      .actions {
        display: flex;
        gap: 0.5rem;
        justify-content: center;
        flex-wrap: wrap;
      }
    `,
  ],
})
export class RegistrationPendingComponent implements OnInit, OnDestroy {
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly http = inject(HttpClient);

  rejected = signal(false);
  reason = signal<string | null>(null);
  private timer: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), 30_000);
  }

  ngOnDestroy(): void {
    if (this.timer) {
      clearInterval(this.timer);
    }
  }

  private async refresh(): Promise<void> {
    const me = await this.auth.bootstrap(true);
    if (!me) {
      return;
    }
    if (me.status === 'registered') {
      // Approved while they waited — straight into first-run.
      void this.router.navigate(['/onboarding']);
      return;
    }
    if (me.status === 'unregistered') {
      void this.router.navigate(['/register']);
      return;
    }
    this.rejected.set(me.status === 'rejected');
    this.reason.set((me.request?.['decision_reason'] as string) ?? null);
  }

  checkNow(): void {
    void this.refresh();
  }

  resubmit(): void {
    void this.router.navigate(['/register']);
  }

  withdraw(): void {
    this.http.delete(`${environment.apiUrl}/registration/mine`).subscribe({
      next: () => void this.router.navigate(['/register']),
      error: () => void this.refresh(),
    });
  }

  signOut(): void {
    this.auth.logout();
  }
}
