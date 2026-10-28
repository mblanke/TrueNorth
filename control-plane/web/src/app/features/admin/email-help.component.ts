import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';

/**
 * Admin → Help → Email: how an operator turns email on (#318). Bundled in the app, so an
 * air-gapped site needs nothing outside it: the "Email is not configured" banner used to
 * link to GitHub. A summary of install/README.md, "Email (SMTP)"; keep the two in step.
 * Static text only, behind the admin guard: the repository's docs/ is never served.
 */
@Component({
  selector: 'tn-email-help',
  imports: [RouterLink, MatCardModule, MatIconModule],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">mail_lock</mat-icon>
          <div>
            <h1>Set up email</h1>
            <p class="subtitle">Roll-call onboarding emails, reminders, seat-forfeit notices and calendar invites</p>
          </div>
        </div>
      </div>

      <mat-card>
        <mat-card-content>
          <p>
            TrueNorth sends all its mail through one SMTP relay, set by whoever runs the installer.
            Email is optional: with no relay, each message is only logged, and Students added to a
            roll call must be told to sign in themselves.
          </p>

          <h2>1. Set the relay in the installer</h2>
          <p>In <code>install/inventory/group_vars/all/main.yml</code> (or the host's inventory) and the vault:</p>
          <table class="vars">
            <thead><tr><th>Variable</th><th>Default</th><th>What it is</th></tr></thead>
            <tbody>
              <tr><td><code>tn_smtp_host</code></td><td>empty: email off</td><td>The relay's name or address, no scheme.</td></tr>
              <tr><td><code>tn_smtp_port</code></td><td>587</td><td>587 with starttls; 465 with tls; 25 with starttls or none.</td></tr>
              <tr><td><code>tn_smtp_security</code></td><td>starttls</td><td>starttls, tls (implicit TLS), or none (an internal relay only, never with a sign-in).</td></tr>
              <tr><td><code>tn_smtp_username</code></td><td>empty: no sign-in</td><td>The relay account, if it needs one.</td></tr>
              <tr><td><code>vault_smtp_password</code></td><td>empty</td><td>Its password, in the vault. Set both or neither.</td></tr>
              <tr><td><code>tn_smtp_from_address</code></td><td>required with a host</td><td>The sender on every message, e.g. truenorth&#64;corp.example.</td></tr>
            </tbody>
          </table>

          <h2>2. Apply it</h2>
          <p>
            Run <code>playbooks/00-preflight.yml</code>, <code>30-config.yml</code> and <code>50-stack-up.yml</code>
            (or the whole <code>site.yml</code>). Preflight names any setting it refuses and never prints the password.
          </p>

          <h2>3. Check it</h2>
          <p>
            The banner goes away once the api restarts with a relay. On
            <a routerLink="/admin">Admin → System Health</a>, the platform administrator sees the relay and can
            <strong>Send test email</strong> to their own address (once a minute, audited). Delivery is proven
            when the message is in the inbox.
          </p>

          <p class="source">The full reference is <code>install/README.md</code>, "Email (SMTP)", in the installer.</p>
        </mat-card-content>
      </mat-card>
    </div>
  `,
  styles: [`
    h2 { font-size: 16px; margin: 20px 0 8px; }
    .vars { border-collapse: collapse; width: 100%; font-size: 13px; }
    .vars th, .vars td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .source { margin-top: 20px; font-size: 12px; color: var(--text-secondary); }
  `],
})
export class EmailHelpComponent {}
