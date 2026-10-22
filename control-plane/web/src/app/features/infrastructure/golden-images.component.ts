import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, inject } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { GoldenImage, GoldenImageAuditEntry, GoldenImageBuild, GoldenImageBuilds } from '@core/models';
import { GoldenImagesApiService } from '@core/services/golden-images-api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

/** "a1b2c3d4…" for a sha256; the full value is in the tooltip. */
export function shortSum(sum: string | null | undefined): string {
  return sum ? `${sum.replace(/^sha256:/, '').slice(0, 12)}…` : '—';
}

/**
 * The golden-image library with its builds (ADR 0013 §2). The template kit registers
 * builds; an admin promotes one, and every new range then clones exactly that build's
 * copy on each host. Promotion is audited; the image's audit trail is shown with its builds.
 */
@Component({
  selector: 'tn-golden-images',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatFormFieldModule, MatIconModule, MatInputModule, MatProgressBarModule,
    MatTableModule, MatTooltipModule, EmptyStateComponent,
  ],
  template: `
    @if (loading) {
      <mat-progress-bar mode="indeterminate" />
    }
    @if (error) {
      <p class="note error" data-testid="gi-error">{{ error }}</p>
    }
    @if (images.length) {
      <p class="note">Ranges clone only an image's <strong>promoted</strong> build, on every host it is placed on.
        A build the template kit registered is not used until an admin promotes it.</p>
      <div class="table-container">
        <table mat-table [dataSource]="images" class="full-width" data-testid="gi-table">
          <ng-container matColumnDef="image">
            <th mat-header-cell *matHeaderCellDef>Image</th>
            <td mat-cell *matCellDef="let i">{{ i.catalogue_id }}</td>
          </ng-container>
          <ng-container matColumnDef="status">
            <th mat-header-cell *matHeaderCellDef>Status</th>
            <td mat-cell *matCellDef="let i">{{ i.enabled ? i.build_status : 'disabled' }}</td>
          </ng-container>
          <ng-container matColumnDef="promoted">
            <th mat-header-cell *matHeaderCellDef>Promoted build</th>
            <td mat-cell *matCellDef="let i" [class.unpromoted]="!i.promoted_build">
              {{ i.promoted_build?.build_id ?? 'none' }}
            </td>
          </ng-container>
          <ng-container matColumnDef="checksum">
            <th mat-header-cell *matHeaderCellDef>Checksum</th>
            <td mat-cell *matCellDef="let i" [matTooltip]="i.promoted_build?.checksum ?? ''">
              {{ short(i.promoted_build?.checksum) }}
            </td>
          </ng-container>
          <ng-container matColumnDef="actions">
            <th mat-header-cell *matHeaderCellDef></th>
            <td mat-cell *matCellDef="let i">
              <button mat-stroked-button [attr.data-testid]="'gi-open-' + i.catalogue_id" (click)="open(i)">
                <mat-icon>history</mat-icon> Builds
              </button>
            </td>
          </ng-container>
          <tr mat-header-row *matHeaderRowDef="cols"></tr>
          <tr mat-row *matRowDef="let row; columns: cols;" [class.selected]="row.id === selected?.id"></tr>
        </table>
      </div>
    } @else if (!loading) {
      <tn-empty-state icon="inventory_2" title="No golden images"
        message="Import the catalogue to list the golden images ranges are built from." />
    }

    @if (selected && detail) {
      <section class="builds" data-testid="gi-builds">
        <h3>{{ selected.catalogue_id }}: builds</h3>
        @if (!detail.builds.length) {
          <p class="note" data-testid="gi-no-builds">No build registered yet. The template kit records one when it
            builds and replicates the image (build-phase.sh, or build.sh with TN_API_URL).</p>
        }
        @for (b of detail.builds; track b.id) {
          <div class="build" [class.is-promoted]="b.promoted" [attr.data-testid]="'gi-build-' + b.build_id">
            <div class="build-head">
              <strong>{{ b.build_id }}</strong>
              @if (b.promoted) {
                <span class="badge" data-testid="gi-promoted-badge">Promoted</span>
              }
              <span class="spacer"></span>
              @if (!b.promoted && canPromote()) {
                <span [matTooltip]="b.promotable ? '' : 'Cannot be promoted: ' + b.not_promotable_reason">
                  <button mat-flat-button color="primary" [attr.data-testid]="'gi-promote-' + b.build_id"
                    [disabled]="promoting || !b.promotable" (click)="promote(b)">
                    <mat-icon>verified</mat-icon> Promote
                  </button>
                </span>
              }
            </div>
            <dl>
              <dt>Checksum</dt><dd class="mono" [matTooltip]="b.checksum">{{ short(b.checksum) }}</dd>
              <dt>Built</dt><dd>{{ b.build_date || '—' }}</dd>
              <dt>ISO checksum</dt><dd class="mono" [matTooltip]="b.iso_checksum">{{ b.iso_checksum ? short(b.iso_checksum) : 'not recorded' }}</dd>
              <dt>Host copies</dt>
              <dd data-testid="gi-copies">{{ copies(b) }}</dd>
              @if (b.missing_hosts?.length) {
                <dt>No copy on</dt>
                <dd class="warn" [attr.data-testid]="'gi-missing-' + b.build_id">{{ b.missing_hosts?.join(', ') }}
                  (placement will not use these hosts for this image)</dd>
              }
              @if (!b.promoted && !b.promotable) {
                <dt>Not promotable</dt>
                <dd class="warn" [attr.data-testid]="'gi-unpromotable-' + b.build_id">{{ b.not_promotable_reason }}</dd>
              }
              <dt>Registered</dt><dd>{{ b.registered_at | date: 'medium' }}{{ b.registered_by ? ' by ' + b.registered_by : '' }}</dd>
              @if (b.promoted) {
                <dt>Promoted</dt><dd>{{ b.promoted_at | date: 'medium' }}{{ b.promoted_by ? ' by ' + b.promoted_by : '' }}</dd>
              }
            </dl>
          </div>
        }
        @if (canPromote() && hasPromotable()) {
          <mat-form-field appearance="outline" subscriptSizing="dynamic" class="reason">
            <mat-label>Reason for promotion (kept in the audit log)</mat-label>
            <input matInput data-testid="gi-reason" [(ngModel)]="reason" maxlength="500" />
          </mat-form-field>
        }
        <h4>Audit</h4>
        @if (detail.audit.length) {
          <ul class="audit" data-testid="gi-audit">
            @for (a of detail.audit; track $index) {
              <li>{{ a.timestamp | date: 'medium' }} · {{ actionLabel(a) }} · {{ a.actor || 'unknown' }}</li>
            }
          </ul>
        } @else {
          <p class="note">No promotion or registration recorded yet.</p>
        }
      </section>
    }
  `,
  styles: [`
    :host { display: block; margin-top: 16px; }
    .note { color: var(--text-secondary); font-size: 13px; margin: 0 0 8px; }
    .note.error { color: var(--mat-sys-error, #d32f2f); }
    .full-width { width: 100%; }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
    td.unpromoted { color: var(--text-secondary) !important; font-style: italic; }
    tr.selected td { font-weight: 600; }
    .builds { margin-top: 20px; }
    .build { border: 1px solid var(--border-color, rgba(127,127,127,.3)); border-radius: 8px; padding: 12px; margin: 8px 0; }
    .build.is-promoted { border-color: var(--mat-sys-primary, #1976d2); }
    .build-head { display: flex; align-items: center; gap: 8px; }
    .spacer { flex: 1; }
    .badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--mat-sys-primary-container, #e3f2fd); }
    dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 8px 0 0; font-size: 13px; }
    dt { color: var(--text-secondary); }
    dd { margin: 0; }
    .mono { font-family: monospace; }
    .reason { width: 100%; margin-top: 8px; }
    .audit { font-size: 13px; padding-left: 18px; }
    .warn { color: var(--mat-sys-error, #d32f2f); }
  `],
})
export class GoldenImagesComponent implements OnInit {
  private api = inject(GoldenImagesApiService);
  private cdr = inject(ChangeDetectorRef);

  readonly cols = ['image', 'status', 'promoted', 'checksum', 'actions'];
  readonly short = shortSum;
  images: GoldenImage[] = [];
  selected: GoldenImage | null = null;
  detail: GoldenImageBuilds | null = null;
  reason = '';
  loading = false;
  promoting = false;
  error = '';

  ngOnInit(): void {
    this.reload();
  }

  /** The platform administrator only: the API says so (can_promote) and refuses anyone else. */
  canPromote(): boolean {
    return !!this.detail?.can_promote;
  }

  hasPromotable(): boolean {
    return !!this.detail?.builds.some(b => !b.promoted && b.promotable);
  }

  /** What promoting `b` changes, for the confirmation. */
  promoteMessage(img: GoldenImage, b: GoldenImageBuild): string {
    const before = this.detail?.promoted_build_id;
    const hosts = Object.keys(b.copies ?? {}).sort().join(', ') || 'no host';
    const lines = [
      `Promote build ${b.build_id} of ${img.catalogue_id}?`,
      before
        ? `It replaces build ${before}: new ranges stop using ${before} (ranges already built keep what they have).`
        : 'It is the first promoted build of this image: new ranges stop using its unverified template.',
      `New ranges clone this build's stamped copy on: ${hosts}.`,
    ];
    const missing = b.missing_hosts ?? [];
    if (missing.length) {
      lines.push(`These hosts have no copy and will not be used for this image: ${missing.join(', ')}.`);
    }
    return lines.join('\n');
  }

  copies(b: GoldenImageBuild): string {
    const hosts = Object.keys(b.copies ?? {}).sort();
    return hosts.length ? hosts.map(h => `${h}: ${b.copies[h]}`).join(', ') : 'none recorded';
  }

  actionLabel(a: GoldenImageAuditEntry): string {
    const build = (a.detail as Record<string, unknown>)?.['build_id'] ?? '';
    if (a.action === 'golden_image.promote') return `promoted ${build}`;
    if (a.action === 'golden_image.build_register') return `registered ${build}`;
    return a.action;
  }

  reload(): void {
    this.loading = true;
    this.api.list().subscribe({
      next: imgs => { this.images = imgs; this.loading = false; this.cdr.markForCheck(); },
      error: e => this.fail('Could not load golden images', e),
    });
  }

  open(img: GoldenImage): void {
    this.selected = img;
    this.detail = null;
    this.reason = '';
    this.api.builds(img.id).subscribe({
      next: d => { this.detail = d; this.cdr.markForCheck(); },
      error: e => this.fail('Could not load builds', e),
    });
  }

  promote(b: GoldenImageBuild): void {
    const img = this.selected;
    if (!img || this.promoting) return;
    const ok = window.confirm(this.promoteMessage(img, b));
    if (!ok) return;
    this.promoting = true;
    this.api.promote(img.id, b.build_id, this.reason.trim()).subscribe({
      next: d => {
        this.detail = d;
        this.promoting = false;
        this.reason = '';
        this.reload();
      },
      error: e => { this.promoting = false; this.fail('Promotion failed', e); },
    });
  }

  private fail(what: string, e: { error?: { detail?: string }; message?: string }): void {
    this.error = `${what}: ${e?.error?.detail || e?.message || 'unknown error'}`;
    this.loading = false;
    this.cdr.markForCheck();
  }
}
