import { Component, computed, inject } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { AuthService } from '@core/services/auth.service';
import { ApprovalsPanelComponent } from '../users/approvals-panel.component';

/**
 * The registration approval queue as a page of its own.
 *
 * Only administrators hold registration:read and registration:approve (rbac.py; ADR 0014
 * §5, #319): instructors' Students come in through their class roll call. Same panel, same
 * API calls (GET /registration/requests, POST .../approve, .../reject, bulk-approve);
 * the API scopes the queue to the approver's tenant and refuses roles they may not grant.
 */
@Component({
  selector: 'tn-approvals',
  imports: [MatIconModule, ApprovalsPanelComponent],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">how_to_reg</mat-icon>
          <div>
            <h1>Registration approvals</h1>
            <p class="subtitle">People who signed in with their directory account and asked for access.
              Approving creates their account with the role you choose.</p>
          </div>
        </div>
      </div>
      @if (role(); as r) {
        <tn-approvals-panel [approverRole]="r" />
      }
    </div>
  `,
})
export class ApprovalsComponent {
  /** The panel offers only the roles this approver may grant. */
  private readonly auth = inject(AuthService);
  protected readonly role = computed(() => this.auth.user()?.role ?? null);
}
