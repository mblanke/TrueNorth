import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AuthService } from '@core/services/auth.service';

@Component({
  selector: 'tn-login',
  standalone: true,
  imports: [CommonModule, MatCardModule, MatButtonModule, MatIconModule],
  template: `
    <div class="login-container">
      <mat-card class="login-card">
        <mat-card-header>
          <mat-icon mat-card-avatar class="logo">security</mat-icon>
          <mat-card-title>TrueNorth Range</mat-card-title>
          <mat-card-subtitle>Cyber Training Platform</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <p>Sign in to access the training environment.</p>
        </mat-card-content>
        <mat-card-actions>
          <button mat-raised-button color="primary" (click)="login()">
            <mat-icon>login</mat-icon> Sign in with SSO
          </button>
        </mat-card-actions>
      </mat-card>
    </div>
  `,
  styles: [`
    .login-container {
      display: flex; justify-content: center; align-items: center;
      height: 100vh; background: var(--bg-primary);
    }
    .login-card { max-width: 400px; padding: 32px; text-align: center; }
    .logo { font-size: 48px !important; width: 48px !important; height: 48px !important; color: var(--accent); }
  `],
})
export class LoginComponent {
  constructor(private auth: AuthService, private router: Router) {
    if (this.auth.isAuthenticated()) {
      this.router.navigate(['/dashboard']);
    }
  }
  login(): void { this.auth.login(); }
}