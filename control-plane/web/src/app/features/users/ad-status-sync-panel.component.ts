import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSnackBar } from '@angular/material/snack-bar';
import {
  AdStatusSyncOverview, AdStatusSyncRun, DirectoryApiService,
} from '@core/services/directory-api.service';

/**
 * The nightly AD status sync (ADR 0014 §6, #290): the last run, its counts, the accounts it
 * deactivated (or, in a dry run, would have), and "Run now" for this tenant.
 *
 * TrueNorth only reads the directory. Deactivating someone frees their roll-call seats and
 * tells their instructors; nothing is deleted. Break-glass is never disabled.
 */
@Component({
  selector: 'tn-ad-status-sync-panel',
  imports: [DatePipe, MatCardModule, MatButtonModule, MatIconModule, MatTableModule, MatChipsModule, MatTooltipModule],
  template: `
    <mat-card class="sync-card">
      <mat-card-header>
        <mat-card-title>Directory status check</mat-card-title>
        <mat-card-subtitle>
          Nightly at {{ pad(view()?.nightly_hour_utc ?? 2) }}:00 UTC. Deactivates accounts whose AD account is
          disabled, gone or in no {{ view()?.group_prefix ?? 'TN-' }}* group, and local accounts unused for 90 days.
          TrueNorth never changes AD, and nothing is deleted.
        </mat-card-subtitle>
      </mat-card-header>
      <mat-card-content>
        @if (error()) {
          <p class="error" role="alert">{{ error() }}</p>
        }
        @if (view(); as v) {
          <div class="facts">
            <span><strong>Mode:</strong> {{ v.dry_run ? 'Dry run (records only, changes nothing)' : 'Applies changes' }}</span>
            @if (!v.keycloak_configured) {
              <span class="warn">Keycloak's Admin API is not configured: the check cannot run.</span>
            }
            @if (!v.enabled) {
              <span class="warn">The nightly check is turned off (AD_STATUS_SYNC_ENABLED).</span>
            }
            <span>
              <strong>Break-glass:</strong>
              {{ v.break_glass_usernames.length ? v.break_glass_usernames.join(', ') : 'none configured (local accounts are not checked)' }}
              — last used {{ v.last_break_glass_use ? (v.last_break_glass_use | date:'medium') : 'never' }}
            </span>
          </div>
          @if (v.latest; as run) {
            <h3 class="run-title">
              Last run: {{ run.started_at | date:'medium' }} ({{ run.trigger }}{{ run.dry_run ? ', dry run' : '' }})
              <mat-chip-set><mat-chip [class]="'status-' + run.status">{{ run.status }}</mat-chip></mat-chip-set>
            </h3>
            @if (run.error) {
              <p class="error">{{ run.error }}</p>
            }
            <div class="counts">
              @for (c of countList(run); track c[0]) {
                <span class="count"><strong>{{ c[1] }}</strong> {{ label(c[0]) }}</span>
              } @empty {
                <span>No accounts in this tenant were looked at.</span>
              }
            </div>
            @if (run.results?.length) {
              <table mat-table [dataSource]="run.results ?? []" class="results">
                <ng-container matColumnDef="person">
                  <th mat-header-cell *matHeaderCellDef>Account</th>
                  <td mat-cell *matCellDef="let r">{{ r.display_name }} &lt;{{ r.email }}&gt;</td>
                </ng-container>
                <ng-container matColumnDef="reason">
                  <th mat-header-cell *matHeaderCellDef>Why</th>
                  <td mat-cell *matCellDef="let r">{{ r.reason_text }}</td>
                </ng-container>
                <ng-container matColumnDef="applied">
                  <th mat-header-cell *matHeaderCellDef>Deactivated</th>
                  <td mat-cell *matCellDef="let r">{{ r.applied ? 'Yes' : 'No (would be)' }}</td>
                </ng-container>
                <ng-container matColumnDef="seats">
                  <th mat-header-cell *matHeaderCellDef>Seats freed</th>
                  <td mat-cell *matCellDef="let r">{{ r.seats_freed }}</td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="columns"></tr>
                <tr mat-row *matRowDef="let row; columns: columns"></tr>
              </table>
            } @else {
              <p>No account in this tenant was deactivated by this run.</p>
            }
          } @else {
            <p>No run yet.</p>
          }
        }
      </mat-card-content>
      <mat-card-actions>
        <button mat-stroked-button (click)="run(true)" [disabled]="busy()"
                matTooltip="Look now and record who would be deactivated; changes nothing">
          <mat-icon>visibility</mat-icon> Dry run now
        </button>
        <button mat-raised-button color="warn" (click)="run()" [disabled]="busy()"
                [matTooltip]="runTip()">
          <mat-icon>sync</mat-icon> Run now
        </button>
        @if (view()?.latest?.status === 'held') {
          <button mat-raised-button color="warn" (click)="run(undefined, true)" [disabled]="busy()"
                  matTooltip="This run would deactivate more accounts than the safety limit allows">
            <mat-icon>warning</mat-icon> Run now, over the limit
          </button>
        }
      </mat-card-actions>
    </mat-card>
  `,
  styles: [`
    .sync-card { max-width: 900px; margin-top: 16px; background: var(--bg-card); }
    .facts { display: flex; flex-direction: column; gap: 6px; margin: 12px 0; }
    .counts { display: flex; flex-wrap: wrap; gap: 16px; margin: 8px 0 16px; }
    .run-title { display: flex; align-items: center; gap: 12px; font-size: 15px; margin: 16px 0 4px; }
    .warn, .error { color: var(--alert); }
    .results { width: 100%; background: transparent; }
  `],
})
export class AdStatusSyncPanelComponent implements OnInit {
  private readonly directory = inject(DirectoryApiService);
  private readonly snack = inject(MatSnackBar);

  readonly view = signal<AdStatusSyncOverview | null>(null);
  readonly error = signal('');
  readonly busy = signal(false);
  readonly columns = ['person', 'reason', 'applied', 'seats'];

  private static readonly LABELS: Record<string, string> = {
    examined: 'accounts looked at',
    ad_accounts: 'AD accounts',
    local_accounts: 'local accounts',
    deactivated: 'deactivated',
    ad_disabled: 'AD account disabled',
    ad_removed: 'AD account gone',
    ad_no_tn_group: 'in no TN-* group',
    local_unused: 'local, unused 90 days',
    break_glass_kept: 'break-glass kept',
    not_a_keycloak_account: 'not a Keycloak account (roster, LTI)',
    local_not_checked_no_break_glass: 'local, not checked (no break-glass set)',
    errors: 'errors',
  };

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.directory.adStatusSync().subscribe({
      next: v => { this.view.set(v); this.error.set(''); },
      error: () => this.error.set('The directory status check could not be loaded.'),
    });
  }

  runTip(): string {
    return this.view()?.dry_run
      ? 'Dry-run mode is on (AD_STATUS_SYNC_DRY_RUN): records only'
      : "Deactivate this tenant's accounts the directory no longer allows (audited)";
  }

  /** dryRun undefined: the server's setting (AD_STATUS_SYNC_DRY_RUN). */
  run(dryRun?: boolean, overrideLimit = false): void {
    this.busy.set(true);
    this.directory.runAdStatusSync(dryRun, overrideLimit).subscribe({
      next: r => {
        this.busy.set(false);
        const n = (r.results ?? []).length;
        const done = r.dry_run ? `Dry run complete: ${n} account(s) would be deactivated`
          : `Run complete: ${n} account(s) deactivated`;
        this.snack.open(
          r.status === 'completed' ? done : `Run ${r.status}: ${r.error ?? ''}`,
          'OK', { duration: 6000 },
        );
        this.load();
      },
      error: () => {
        this.busy.set(false);
        this.snack.open('The directory status check could not run', 'OK', { duration: 5000 });
      },
    });
  }

  countList(run: AdStatusSyncRun): [string, number][] {
    return Object.entries(run.counts ?? {});
  }

  label(key: string): string {
    return AdStatusSyncPanelComponent.LABELS[key] ?? key;
  }

  pad(h: number): string {
    return String(h).padStart(2, '0');
  }
}
