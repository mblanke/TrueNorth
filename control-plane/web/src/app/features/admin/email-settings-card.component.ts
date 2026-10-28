import { Component, OnInit, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { RouterLink } from '@angular/router';
import { EMAIL_DOCS_URL, EmailStatusService } from '@core/services/email-status.service';
import { apiError } from '../ranges/range-capabilities';

/**
 * Administration → System Health: is email on, through which relay (the platform
 * administrator only; never the password), and a test message to your own address
 * (POST /email/test: at most once a minute, audited). #306.
 */
@Component({
  selector: 'tn-email-settings-card',
  imports: [MatButtonModule, MatCardModule, MatIconModule, RouterLink],
  template: `
    @if (email.status(); as s) {
      <mat-card class="email-card">
        <mat-card-header>
          <mat-icon mat-card-avatar>{{ s.configured ? 'mark_email_read' : 'mail_lock' }}</mat-icon>
          <mat-card-title>Email</mat-card-title>
          <mat-card-subtitle>{{ s.configured ? 'Configured' : 'Not configured: nothing is emailed' }}</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          @if (s.relay; as r) {
            <dl class="relay">
              <dt>Relay</dt><dd>{{ r.host }}:{{ r.port }} ({{ r.security }})</dd>
              <dt>Sender</dt><dd>{{ r.from_address || '—' }}</dd>
              <dt>Account</dt><dd>{{ r.signs_in ? r.username : 'none (no sign-in)' }}</dd>
            </dl>
          }
          @if (!s.configured) {
            <p>Set the relay in the installer (<a [routerLink]="docsUrl">how to set up email</a>),
              then re-run 30-config and 50-stack-up.</p>
          }
          @if (result()) { <p class="result" role="status">{{ result() }}</p> }
        </mat-card-content>
        @if (s.can_send_test) {
          <mat-card-actions>
            <button mat-stroked-button (click)="sendTest()" [disabled]="sending()">
              <mat-icon>send</mat-icon> Send test email
            </button>
            <span class="hint">To your own address.</span>
          </mat-card-actions>
        }
      </mat-card>
    }
  `,
  styles: [`
    .email-card { margin-top: 16px; }
    .relay { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; }
    .relay dt { font-weight: 600; }
    .relay dd { margin: 0; }
    .hint { margin-left: 8px; font-size: 12px; opacity: 0.8; }
    .result { margin-top: 8px; }
  `],
})
export class EmailSettingsCardComponent implements OnInit {
  readonly email = inject(EmailStatusService);
  readonly docsUrl = EMAIL_DOCS_URL;
  readonly sending = signal(false);
  readonly result = signal('');

  ngOnInit(): void {
    this.email.load(true);
  }

  sendTest(): void {
    this.sending.set(true);
    this.result.set('');
    this.email.sendTest().subscribe({
      next: r => { this.sending.set(false); this.result.set(r.detail); },
      error: err => { this.sending.set(false); this.result.set(apiError(err)); },
    });
  }
}
