// Author: Marc-Andre Blanke
// Created: 2026-10-22T02:24:15Z
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, inject } from '@angular/core';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatDialog } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { GoldenImage, GoldenImageAuditEntry, GoldenImageBuild, GoldenImageBuilds } from '@core/models';
import { GoldenImagesApiService } from '@core/services/golden-images-api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { PromoteBuildDialogComponent, PromoteBuildDialogData } from './promote-build-dialog.component';

/** "a1b2c3d4…" for a sha256; the full value is in the tooltip. */
export function shortSum(sum: string | null | undefined): string {
  return sum ? `${sum.replace(/^sha256:/, '').slice(0, 12)}…` : '—';
}

export type ImageStatus = 'built' | 'building' | 'planned' | 'failed' | 'disabled';
export type StatusFilter = 'all' | ImageStatus;
export type PromotedFilter = 'all' | 'promoted' | 'not_promoted';
export type ImageSort = 'action' | 'name' | 'status';

/** The status the list shows: a disabled image is "disabled" whatever its build state. */
export function imageStatus(i: GoldenImage): string {
  return i.enabled ? i.build_status : 'disabled';
}

/** Built, enabled, and no build promoted yet: ranges cannot use it until an admin promotes one. */
export function needsPromotion(i: GoldenImage): boolean {
  return i.enabled && i.build_status === 'built' && !i.promoted_build;
}

// "Needs action first": what an admin can act on, then what is broken, then what is in hand.
const STATUS_RANK: Record<string, number> = { failed: 1, building: 2, planned: 3, built: 4, disabled: 5 };

// "Status": in the order an image moves through, so built ones come first.
const STATUS_ORDER: Record<string, number> = { built: 0, building: 1, planned: 2, failed: 3, disabled: 4 };

function rank(i: GoldenImage): number {
  return needsPromotion(i) ? 0 : STATUS_RANK[imageStatus(i)] ?? 4;
}

/** The images that pass both filters, in the chosen order (name breaks ties). */
export function viewImages(images: GoldenImage[], status: StatusFilter, promoted: PromotedFilter,
                           sort: ImageSort): GoldenImage[] {
  const byName = (a: GoldenImage, b: GoldenImage) => a.catalogue_id.localeCompare(b.catalogue_id);
  const order: Record<ImageSort, (a: GoldenImage, b: GoldenImage) => number> = {
    action: (a, b) => rank(a) - rank(b) || byName(a, b),
    status: (a, b) => (STATUS_ORDER[imageStatus(a)] ?? 5) - (STATUS_ORDER[imageStatus(b)] ?? 5) || byName(a, b),
    name: byName,
  };
  return images
    .filter(i => status === 'all' || imageStatus(i) === status)
    .filter(i => promoted === 'all' || (promoted === 'promoted') === !!i.promoted_build)
    .sort(order[sort]);
}

/** A failed promotion, in plain words. */
export function promoteErrorText(e: Partial<HttpErrorResponse> | null | undefined): string {
  const raw = e?.error?.detail;
  const detail = typeof raw === 'string' ? raw
    : Array.isArray(raw) ? raw.map((d: { msg?: string }) => d?.msg ?? '').filter(Boolean).join('; ') : '';
  switch (e?.status) {
    case 0: return 'The server could not be reached. Check the connection and try again.';
    case 401: return 'Your sign-in has expired. Sign in again, then try again.';
    case 403: return 'Only the platform administrator can promote a build.';
    case 404: return 'This image or build no longer exists. Reload the page.';
    case 409: return 'Someone promoted a build of this image at the same time. Reload the page and try again.';
    case 422: return `This build cannot be promoted${detail ? ': ' + detail : '.'}`;
    default: return `The build was not promoted${detail ? ': ' + detail : '. Try again.'}`;
  }
}

/**
 * The golden-image library with its builds (ADR 0013 §2). The template kit registers
 * builds; an admin promotes one, and every new range then clones exactly that build's
 * copy on each host. Promotion is audited; the image's audit trail is shown with its builds.
 * Each image row opens in place to show its builds; the list filters by status and by
 * promoted, and by default puts images waiting for a promotion first (#527).
 */
@Component({
  selector: 'tn-golden-images',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DatePipe, NgTemplateOutlet, FormsModule, MatButtonModule, MatFormFieldModule, MatIconModule, MatProgressBarModule,
    MatSelectModule, MatTableModule, MatTooltipModule, EmptyStateComponent,
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
      <div class="filters">
        <mat-form-field appearance="outline" subscriptSizing="dynamic">
          <mat-label>Status</mat-label>
          <mat-select data-testid="gi-filter-status" [(ngModel)]="statusFilter" (selectionChange)="applyView()">
            <mat-option value="all">All</mat-option>
            <mat-option value="built">Built</mat-option>
            <mat-option value="building">Building</mat-option>
            <mat-option value="planned">Planned</mat-option>
            <mat-option value="failed">Failed</mat-option>
            <mat-option value="disabled">Disabled</mat-option>
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline" subscriptSizing="dynamic">
          <mat-label>Promoted</mat-label>
          <mat-select data-testid="gi-filter-promoted" [(ngModel)]="promotedFilter" (selectionChange)="applyView()">
            <mat-option value="all">All</mat-option>
            <mat-option value="promoted">Promoted</mat-option>
            <mat-option value="not_promoted">Not promoted</mat-option>
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline" subscriptSizing="dynamic">
          <mat-label>Sort by</mat-label>
          <mat-select data-testid="gi-sort" [(ngModel)]="sortBy" (selectionChange)="applyView()">
            <mat-option value="action">Needs action first</mat-option>
            <mat-option value="name">Name</mat-option>
            <mat-option value="status">Status</mat-option>
          </mat-select>
        </mat-form-field>
        <span class="count" data-testid="gi-count">{{ rows.length }} of {{ images.length }}
          · {{ waiting() }} waiting for promotion</span>
      </div>
      @if (!rows.length) {
        <p class="note" data-testid="gi-no-match">No image matches these filters.</p>
      }
      <div class="table-container">
        <table mat-table [dataSource]="rows" multiTemplateDataRows class="full-width" data-testid="gi-table">
          <ng-container matColumnDef="image">
            <th mat-header-cell *matHeaderCellDef>Image</th>
            <td mat-cell *matCellDef="let i">{{ i.catalogue_id }}</td>
          </ng-container>
          <ng-container matColumnDef="status">
            <th mat-header-cell *matHeaderCellDef>Status</th>
            <td mat-cell *matCellDef="let i">{{ status(i) }}</td>
          </ng-container>
          <ng-container matColumnDef="promoted">
            <th mat-header-cell *matHeaderCellDef>Promoted build</th>
            <td mat-cell *matCellDef="let i" [class.unpromoted]="!i.promoted_build">
              {{ i.promoted_build?.build_id ?? 'none' }}
              @if (needsPromotion(i)) {
                <span class="badge action" [attr.data-testid]="'gi-needs-' + i.catalogue_id">Needs promotion</span>
              }
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
              <button mat-stroked-button [attr.data-testid]="'gi-open-' + i.catalogue_id"
                [attr.aria-expanded]="i.id === selected?.id" (click)="open(i)">
                <mat-icon>{{ i.id === selected?.id ? 'expand_less' : 'expand_more' }}</mat-icon> Builds
              </button>
            </td>
          </ng-container>
          <ng-container matColumnDef="detail">
            <td mat-cell *matCellDef="let i" [attr.colspan]="cols.length" class="detail-cell">
              @if (i.id === selected?.id) {
                <ng-container [ngTemplateOutlet]="buildsPanel" />
              }
            </td>
          </ng-container>
          <tr mat-header-row *matHeaderRowDef="cols"></tr>
          <tr mat-row *matRowDef="let row; columns: cols;" [class.selected]="row.id === selected?.id"></tr>
          <tr mat-row *matRowDef="let row; columns: ['detail']" class="detail-row"
            [class.open]="row.id === selected?.id"></tr>
        </table>
      </div>
    } @else if (!loading) {
      <tn-empty-state icon="inventory_2" title="No golden images"
        message="Import the catalogue to list the golden images ranges are built from." />
    }

    <ng-template #buildsPanel>
      @if (buildsLoading) {
        <mat-progress-bar mode="indeterminate" />
      }
      @if (buildsError) {
        <p class="note error" data-testid="gi-builds-error">{{ buildsError }}</p>
      }
      @if (selected && detail) {
      <section class="builds" data-testid="gi-builds">
        <h3>{{ selected.catalogue_id }}: builds</h3>
        @if (promoteError) {
          <p class="note error" role="alert" data-testid="gi-promote-error">{{ promoteError }}</p>
        }
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
    </ng-template>
  `,
  styles: [`
    :host { display: block; margin-top: 16px; }
    .filters { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 16px 0 12px; }
    .filters mat-form-field { width: 190px; }
    .count { color: var(--text-secondary); font-size: 13px; }
    tr.detail-row { height: 0; }
    tr.detail-row:not(.open) td { padding: 0; border-bottom-width: 0; }
    td.detail-cell { padding: 0 16px; }
    .badge.action { margin-left: 8px; font-style: normal; color: var(--mat-sys-on-error-container, #410002);
      background: var(--mat-sys-error-container, #ffdad6); }
    .note { color: var(--text-secondary); font-size: 13px; margin: 0 0 8px; }
    .note.error { color: var(--mat-sys-error, #d32f2f); }
    .full-width { width: 100%; }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
    td.unpromoted { color: var(--text-secondary) !important; font-style: italic; }
    tr.selected td { font-weight: 600; }
    .builds { margin: 12px 0 16px; }
    .build { border: 1px solid var(--border-color, rgba(127,127,127,.3)); border-radius: 8px; padding: 12px; margin: 8px 0; }
    .build.is-promoted { border-color: var(--mat-sys-primary, #1976d2); }
    .build-head { display: flex; align-items: center; gap: 8px; }
    .spacer { flex: 1; }
    .badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--mat-sys-primary-container, #e3f2fd); }
    dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 8px 0 0; font-size: 13px; }
    dt { color: var(--text-secondary); }
    dd { margin: 0; }
    .mono { font-family: monospace; }
    .audit { font-size: 13px; padding-left: 18px; }
    .warn { color: var(--mat-sys-error, #d32f2f); }
  `],
})
export class GoldenImagesComponent implements OnInit {
  private api = inject(GoldenImagesApiService);
  private cdr = inject(ChangeDetectorRef);
  private dialog = inject(MatDialog);

  readonly cols = ['image', 'status', 'promoted', 'checksum', 'actions'];
  readonly short = shortSum;
  readonly status = imageStatus;
  readonly needsPromotion = needsPromotion;
  images: GoldenImage[] = [];
  /** `images` filtered and sorted, as the table shows them. */
  rows: GoldenImage[] = [];
  statusFilter: StatusFilter = 'all';
  promotedFilter: PromotedFilter = 'all';
  sortBy: ImageSort = 'action';
  selected: GoldenImage | null = null;
  detail: GoldenImageBuilds | null = null;
  loading = false;
  buildsLoading = false;
  promoting = false;
  error = '';
  buildsError = '';
  promoteError = '';

  ngOnInit(): void {
    this.reload();
  }

  applyView(): void {
    this.rows = viewImages(this.images, this.statusFilter, this.promotedFilter, this.sortBy);
    this.cdr.markForCheck();
  }

  waiting(): number {
    return this.images.filter(needsPromotion).length;
  }

  /** The platform administrator only: the API says so (can_promote) and refuses anyone else. */
  canPromote(): boolean {
    return !!this.detail?.can_promote;
  }

  /** What promoting `b` changes, for the confirmation. */
  promoteLines(img: GoldenImage, b: GoldenImageBuild): string[] {
    const before = this.detail?.promoted_build_id;
    const hosts = Object.keys(b.copies ?? {}).sort().join(', ') || 'no host';
    const lines = [
      `Image: ${img.catalogue_id}.`,
      before
        ? `It replaces build ${before}: new ranges stop using ${before} (ranges already built keep what they have).`
        : 'It is the first promoted build of this image: new ranges stop using its unverified template.',
      `New ranges clone this build's stamped copy on: ${hosts}.`,
    ];
    const missing = b.missing_hosts ?? [];
    if (missing.length) {
      lines.push(`These hosts have no copy and will not be used for this image: ${missing.join(', ')}.`);
    }
    return lines;
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
      next: imgs => { this.images = imgs; this.loading = false; this.error = ''; this.applyView(); },
      error: e => this.fail('Could not load golden images', e),
    });
  }

  /** Opens the image's builds under its row; a second click closes them. */
  open(img: GoldenImage): void {
    if (this.selected?.id === img.id) {
      this.selected = null;
      this.detail = null;
      this.cdr.markForCheck();
      return;
    }
    this.selected = img;
    this.detail = null;
    this.buildsError = '';
    this.promoteError = '';
    this.buildsLoading = true;
    this.api.builds(img.id).subscribe({
      next: d => {
        if (this.selected?.id !== img.id) return;
        this.detail = d;
        this.buildsLoading = false;
        this.cdr.markForCheck();
      },
      error: () => {
        this.buildsLoading = false;
        this.buildsError = 'The builds of this image could not be loaded. Try again.';
        this.cdr.markForCheck();
      },
    });
  }

  /** Confirm with a reason, promote, then refresh the builds and the list. */
  promote(b: GoldenImageBuild): void {
    const img = this.selected;
    if (!img || this.promoting) return;
    const data: PromoteBuildDialogData = { imageName: img.catalogue_id, buildId: b.build_id,
                                           lines: this.promoteLines(img, b) };
    this.dialog.open<PromoteBuildDialogComponent, PromoteBuildDialogData, string>(
      PromoteBuildDialogComponent, { data, width: '560px', maxWidth: '95vw' },
    ).afterClosed().subscribe(reason => {
      if (!reason) return;
      this.promoting = true;
      this.promoteError = '';
      this.cdr.markForCheck();
      this.api.promote(img.id, b.build_id, reason).subscribe({
        next: d => {
          this.detail = d;
          this.promoting = false;
          this.reload();
        },
        error: e => {
          this.promoting = false;
          this.promoteError = promoteErrorText(e);
          this.cdr.markForCheck();
        },
      });
    });
  }

  private fail(what: string, e: { error?: { detail?: string }; message?: string }): void {
    this.error = `${what}: ${e?.error?.detail || e?.message || 'unknown error'}`;
    this.loading = false;
    this.cdr.markForCheck();
  }
}
