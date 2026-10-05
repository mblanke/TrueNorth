import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MAT_DIALOG_DATA, MatDialog, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';

import {
  CoursePublication, CourseRelease, CourseStudioApiService, LearningPlatform,
} from '@core/services/course-studio-api.service';
import { NotificationService } from '@core/services/notification.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

/** Accepting a release: every open ARC² action must be acknowledged, by id. */
@Component({
  selector: 'tn-accept-release-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatButtonModule, MatCheckboxModule, MatDialogModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>Accept {{ data.catalogue_code }} v{{ data.version }}</h2>
    <mat-dialog-content>
      <p>Its content becomes the course's. Students who started earlier keep their attempts.</p>
      @if (data.open_actions.length) {
        <p class="label">Open actions to acknowledge</p>
        @for (a of data.open_actions; track a.id) {
          <mat-checkbox [(ngModel)]="acknowledged[a.id]">
            <span class="cat">{{ a.category }}</span> {{ a.text }}
          </mat-checkbox>
        }
      }
      <mat-form-field appearance="outline" class="full">
        <mat-label>Notes</mat-label>
        <textarea matInput rows="2" [(ngModel)]="notes"></textarea>
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!allAcknowledged()" (click)="accept()">Accept</button>
    </mat-dialog-actions>
  `,
  styles: [`
    mat-dialog-content { display: flex; flex-direction: column; gap: 6px; min-width: min(560px, 80vw); }
    .label { margin: 8px 0 0; font-weight: 600; }
    .cat { font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); margin-right: 4px; }
    .full { width: 100%; margin-top: 12px; }
  `],
})
export class AcceptReleaseDialogComponent {
  readonly data = inject<CourseRelease>(MAT_DIALOG_DATA);
  private readonly ref = inject(MatDialogRef<AcceptReleaseDialogComponent>);
  acknowledged: Record<string, boolean> = {};
  notes = '';

  allAcknowledged(): boolean {
    return this.data.open_actions.every(a => this.acknowledged[a.id]);
  }

  accept(): void {
    this.ref.close({ acknowledge_actions: Object.keys(this.acknowledged).filter(k => this.acknowledged[k]), notes: this.notes });
  }
}

/** Publishing: pick one of the tenant's Moodles. */
@Component({
  selector: 'tn-publish-release-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatButtonModule, MatDialogModule, MatFormFieldModule, MatSelectModule],
  template: `
    <h2 mat-dialog-title>Publish to Moodle</h2>
    <mat-dialog-content>
      <p>The course is built hidden, checked, and only then made live.</p>
      <mat-form-field appearance="outline" class="full">
        <mat-label>Moodle</mat-label>
        <mat-select [(ngModel)]="platformId">
          @for (p of data.platforms; track p.id) {
            <mat-option [value]="p.id">{{ p.name }}</mat-option>
          }
        </mat-select>
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!platformId" [mat-dialog-close]="platformId">Publish</button>
    </mat-dialog-actions>
  `,
  styles: [`.full { width: 100%; } mat-dialog-content { min-width: min(420px, 80vw); }`],
})
export class PublishReleaseDialogComponent {
  readonly data = inject<{ platforms: LearningPlatform[] }>(MAT_DIALOG_DATA);
  platformId = this.data.platforms.length === 1 ? this.data.platforms[0].id : '';
}

/**
 * Authoring / Courses — ARC² course releases: upload a release built by
 * `python -m arc2.release build`, accept it (its content becomes the course's), publish it
 * to Moodle, download the instructor pack.
 */
@Component({
  selector: 'tn-course-studio',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatCardModule, MatIconModule, MatTooltipModule, EmptyStateComponent],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">school</mat-icon>
          <div>
            <h1>Courses</h1>
            <p class="subtitle">Releases from ARC²: accept one to make it the course, then publish it to Moodle.</p>
          </div>
        </div>
        <input #fileInput type="file" accept=".tar.gz,.tgz,application/gzip" (change)="upload($event)" hidden>
        <button mat-raised-button color="primary" (click)="fileInput.click()">
          <mat-icon>upload</mat-icon> Upload release
        </button>
      </div>

      @if (loading()) {
        <div class="tn-skeleton-group" aria-busy="true">
          <div class="tn-skeleton tn-skeleton-card"></div>
          <div class="tn-skeleton tn-skeleton-card"></div>
        </div>
      } @else {
        <div class="release-list">
          @for (r of releases(); track r.id) {
            <mat-card class="release">
              <div class="row">
                <div class="who">
                  <span class="code">{{ r.catalogue_code }}</span>
                  <span class="title">{{ r.title }}</span>
                  <span class="version">v{{ r.version }}</span>
                </div>
                <span class="status-chip" [class.stable]="r.state === 'accepted'" [class.draft]="r.state === 'candidate'">
                  {{ r.state }}
                </span>
              </div>
              <div class="row meta">
                <span class="kinds">{{ kinds(r) }}</span>
                @if (r.open_actions.length) {
                  <span class="actions-open" [matTooltip]="actionsTooltip(r)">
                    <mat-icon aria-hidden="true">flag</mat-icon>{{ r.open_actions.length }} open action(s)
                  </span>
                }
                @for (p of publicationsOf(r.id); track p.id) {
                  <span class="status-chip" [class.stable]="p.state === 'published'" [class.failed]="p.state === 'failed'"
                        [matTooltip]="p.error || ''">Moodle: {{ p.state }}</span>
                  @if (p.state === 'failed') {
                    <button mat-button (click)="retry(p)"><mat-icon>refresh</mat-icon> Retry</button>
                  }
                }
              </div>
              <div class="row buttons">
                @if (r.state === 'candidate') {
                  <button mat-stroked-button color="primary" (click)="accept(r)"><mat-icon>task_alt</mat-icon> Accept</button>
                }
                @if (r.state === 'accepted') {
                  <button mat-stroked-button color="primary" (click)="publish(r)" [disabled]="!platforms().length"
                          [matTooltip]="platforms().length ? '' : 'Register a Moodle under Integrations first'">
                    <mat-icon>cloud_upload</mat-icon> Publish
                  </button>
                }
                <button mat-button (click)="downloadPack(r)"><mat-icon>download</mat-icon> Instructor pack</button>
                @if (r.state === 'accepted' && !platforms().length) {
                  <span class="hint">Register a Moodle under Integrations to publish.</span>
                }
              </div>
            </mat-card>
          } @empty {
            <tn-empty-state icon="school" title="No course releases yet"
              message="Build one from an accepted ARC² run with python -m arc2.release build, then upload it here." />
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .header-left { display: flex; align-items: center; gap: 16px; }
    .page-icon { font-size: 32px; width: 32px; height: 32px; color: var(--accent); }
    h1 { margin: 0; font-size: 24px; }
    .release-list { display: flex; flex-direction: column; gap: 12px; }
    .release { border: 1px solid var(--border); padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
    .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .row:first-child { justify-content: space-between; }
    .who { display: flex; align-items: baseline; gap: 10px; }
    .code { font-family: var(--font-mono); font-weight: 600; }
    .title { font-size: 1rem; }
    .version, .kinds { color: var(--text-muted); font-size: 0.85rem; }
    .actions-open { display: inline-flex; align-items: center; gap: 4px; color: var(--severity-medium, #b26a00); font-size: 0.85rem; }
    .actions-open mat-icon { font-size: 16px; width: 16px; height: 16px; }
    .buttons { gap: 6px; }
    .hint { color: var(--text-muted); font-size: 0.85rem; }
  `],
})
export class CourseStudioComponent implements OnInit {
  private readonly api = inject(CourseStudioApiService);
  private readonly notify = inject(NotificationService);
  private readonly dialog = inject(MatDialog);

  readonly loading = signal(true);
  readonly releases = signal<CourseRelease[]>([]);
  readonly platforms = signal<LearningPlatform[]>([]);
  private readonly publications = signal<Record<string, CoursePublication[]>>({});

  ngOnInit(): void {
    this.load();
    this.api.platforms().subscribe({
      next: p => this.platforms.set((p ?? []).filter(x => x.platform_type === 'moodle' && x.is_active)),
      error: () => this.platforms.set([]),
    });
  }

  load(): void {
    this.api.releases().subscribe({
      next: r => {
        this.releases.set(r ?? []);
        this.loading.set(false);
        for (const rel of r ?? []) {
          if (rel.state === 'accepted') {
            this.loadPublications(rel.id);
          }
        }
      },
      error: () => {
        this.loading.set(false);
        this.notify.error('Could not load course releases');
      },
    });
  }

  private loadPublications(releaseId: string): void {
    this.api.publications(releaseId).subscribe({
      next: p => this.publications.update(all => ({ ...all, [releaseId]: p ?? [] })),
    });
  }

  publicationsOf(releaseId: string): CoursePublication[] {
    return this.publications()[releaseId] ?? [];
  }

  kinds(r: CourseRelease): string {
    const counts: Record<string, number> = {};
    for (const kind of Object.values(r.activities ?? {})) {
      counts[kind] = (counts[kind] ?? 0) + 1;
    }
    return ['theory', 'practical', 'range']
      .filter(k => counts[k])
      .map(k => `${counts[k]} ${k}`)
      .join(' · ');
  }

  actionsTooltip(r: CourseRelease): string {
    return r.open_actions.map(a => `${a.category}: ${a.text}`).join('\n');
  }

  upload(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) {
      return;
    }
    this.api.upload(file).subscribe({
      next: r => {
        this.notify.success(`${r.catalogue_code} v${r.version} uploaded`);
        this.load();
      },
      error: err => this.notify.error(err?.error?.detail ?? 'The release was refused'),
    });
  }

  accept(r: CourseRelease): void {
    this.dialog.open(AcceptReleaseDialogComponent, { data: r }).afterClosed().subscribe(body => {
      if (!body) {
        return;
      }
      this.api.accept(r.id, body).subscribe({
        next: () => {
          this.notify.success(`${r.catalogue_code} v${r.version} accepted`);
          this.load();
        },
        error: err => this.notify.error(err?.error?.detail ?? 'Could not accept the release'),
      });
    });
  }

  publish(r: CourseRelease): void {
    this.dialog.open(PublishReleaseDialogComponent, { data: { platforms: this.platforms() } }).afterClosed()
      .subscribe((platformId: string | undefined) => {
        if (!platformId) {
          return;
        }
        this.api.publish(r.id, platformId).subscribe({
          next: () => {
            this.notify.info('Publishing: the course is being built in Moodle');
            this.loadPublications(r.id);
          },
          error: err => this.notify.error(err?.error?.detail ?? 'Could not publish'),
        });
      });
  }

  retry(p: CoursePublication): void {
    this.api.retryPublication(p.id).subscribe({
      next: () => this.loadPublications(p.release_id),
      error: err => this.notify.error(err?.error?.detail ?? 'Could not retry'),
    });
  }

  downloadPack(r: CourseRelease): void {
    this.api.instructorBundle(r.id).subscribe({
      next: blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${r.catalogue_code.replace(/[^A-Za-z0-9_.-]/g, '_')}-v${r.version}-instructor.tar.gz`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: () => this.notify.error('Could not download the instructor pack'),
    });
  }
}
