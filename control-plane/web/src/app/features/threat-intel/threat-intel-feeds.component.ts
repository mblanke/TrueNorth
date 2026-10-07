import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Observable } from 'rxjs';
import { FeedPull, ThreatFeed, ThreatIntelApiService } from '@core/services/threat-intel-api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

/** The API answers a refused pull with detail as a string. */
function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}

/**
 * Threat intel feeds under Authoring › Detections: the feed list, "Pull now" for a feed
 * with a URL, and "Upload CSV" for one without. The last pull's counts and the first
 * rejected rows are shown, so an author can fix the feed rather than guess.
 */
@Component({
  selector: 'tn-threat-intel-feeds',
  imports: [
    DatePipe,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatSnackBarModule,
    MatTableModule,
    MatTooltipModule,
    EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">travel_explore</mat-icon>
          <div>
            <h1>Threat intel feeds</h1>
            <p class="subtitle">Indicator feeds for this tenant. CSV columns: type, value, first_seen, mitre_technique.</p>
          </div>
        </div>
      </div>

      <mat-card class="new-feed">
        <mat-card-content>
          <form class="new-feed-row" (ngSubmit)="addFeed()">
            <mat-form-field appearance="outline">
              <mat-label>Feed name</mat-label>
              <input matInput name="name" [(ngModel)]="newName" required />
            </mat-form-field>
            <mat-form-field appearance="outline" class="grow">
              <mat-label>CSV URL (leave empty to upload)</mat-label>
              <input matInput name="url" [(ngModel)]="newUrl" placeholder="https://feeds.example.org/iocs.csv" />
            </mat-form-field>
            <button mat-raised-button color="primary" type="submit" [disabled]="!newName.trim() || creating()">
              <mat-icon>add</mat-icon> Add feed
            </button>
          </form>
        </mat-card-content>
      </mat-card>

      @if (loading()) {
        <mat-spinner diameter="32" aria-label="Loading feeds"></mat-spinner>
      } @else if (!feeds().length) {
        <tn-empty-state icon="travel_explore" title="No feeds yet"
          message="Add a CSV feed by URL, or add one without a URL and upload its file." />
      } @else {
        <table mat-table [dataSource]="feeds()" class="feeds">
          <ng-container matColumnDef="name">
            <th mat-header-cell *matHeaderCellDef>Feed</th>
            <td mat-cell *matCellDef="let f">{{ f.name }} <span class="type">{{ f.feed_type }}</span></td>
          </ng-container>
          <ng-container matColumnDef="indicators">
            <th mat-header-cell *matHeaderCellDef>Indicators</th>
            <td mat-cell *matCellDef="let f">{{ f.indicator_count }}</td>
          </ng-container>
          <ng-container matColumnDef="last">
            <th mat-header-cell *matHeaderCellDef>Last pull</th>
            <td mat-cell *matCellDef="let f">
              @if (f.last_poll_at) {
                <span [class.bad]="f.last_poll_status?.startsWith('error')">{{ f.last_poll_status }}</span>
                · {{ f.last_poll_at | date: 'short' }}
              } @else { never }
            </td>
          </ng-container>
          <ng-container matColumnDef="actions">
            <th mat-header-cell *matHeaderCellDef></th>
            <td mat-cell *matCellDef="let f" class="actions">
              @if (f.url) {
                <button mat-stroked-button (click)="pull(f)" [disabled]="busy() === f.id || !f.is_enabled"
                        [attr.aria-label]="'Pull ' + f.name + ' now'">
                  <mat-icon>sync</mat-icon> Pull now
                </button>
              }
              <button mat-stroked-button (click)="fileInput.click()" [disabled]="busy() === f.id || !f.is_enabled"
                      [attr.aria-label]="'Upload a CSV for ' + f.name">
                <mat-icon>upload_file</mat-icon> Upload CSV
              </button>
              <input #fileInput type="file" accept=".csv,text/csv" hidden (change)="onFile(f, fileInput)" />
            </td>
          </ng-container>
          <tr mat-header-row *matHeaderRowDef="columns"></tr>
          <tr mat-row *matRowDef="let row; columns: columns"></tr>
        </table>
      }

      @if (lastPull(); as p) {
        <mat-card class="result" role="status">
          <mat-card-content>
            <strong>{{ p.feed.name }}</strong>: {{ p.status }} —
            {{ p.created }} created, {{ p.updated }} updated, {{ p.deactivated }} deactivated, {{ p.rejected }} rejected
            @if (p.rejections.length) {
              <ul class="rejections">
                @for (r of p.rejections; track r.row) {
                  <li>Row {{ r.row }}: {{ r.reason }}</li>
                }
              </ul>
            }
          </mat-card-content>
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .new-feed { margin-bottom: 16px; }
    .new-feed-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .new-feed-row .grow { flex: 1 1 280px; }
    .feeds { width: 100%; }
    .type { color: var(--text-secondary); font-size: 12px; margin-left: 6px; }
    .actions { display: flex; gap: 8px; justify-content: flex-end; }
    .bad { color: var(--alert); }
    .result { margin-top: 16px; }
    .rejections { margin: 8px 0 0; padding-left: 18px; }
  `],
})
export class ThreatIntelFeedsComponent implements OnInit {
  private readonly api = inject(ThreatIntelApiService);
  private readonly snackBar = inject(MatSnackBar);

  readonly columns = ['name', 'indicators', 'last', 'actions'];
  feeds = signal<ThreatFeed[]>([]);
  loading = signal(true);
  creating = signal(false);
  busy = signal<string | null>(null);
  lastPull = signal<FeedPull | null>(null);

  newName = '';
  newUrl = '';

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.feeds().subscribe({
      next: feeds => { this.feeds.set(feeds); this.loading.set(false); },
      error: () => {
        this.loading.set(false);
        this.snackBar.open('Failed to load feeds', 'Close', { duration: 3000 });
      },
    });
  }

  addFeed(): void {
    const name = this.newName.trim();
    if (!name) return;
    this.creating.set(true);
    this.api.createFeed({ name, feed_type: 'csv', url: this.newUrl.trim() || null }).subscribe({
      next: feed => {
        this.feeds.update(list => [...list, feed].sort((a, b) => a.name.localeCompare(b.name)));
        this.newName = '';
        this.newUrl = '';
        this.creating.set(false);
      },
      error: err => {
        this.creating.set(false);
        this.snackBar.open(errorMessage(err, 'Failed to add feed'), 'Close', { duration: 5000 });
      },
    });
  }

  pull(feed: ThreatFeed): void {
    this.run(feed, this.api.pull(feed.id));
  }

  onFile(feed: ThreatFeed, input: HTMLInputElement): void {
    const file = input.files?.[0];
    input.value = '';  // the same file can be chosen again after a fix
    if (file) this.run(feed, this.api.upload(feed.id, file));
  }

  private run(feed: ThreatFeed, call: Observable<FeedPull>): void {
    this.busy.set(feed.id);
    call.subscribe({
      next: result => {
        this.busy.set(null);
        this.lastPull.set(result);
        this.feeds.update(list => list.map(f => (f.id === result.feed.id ? result.feed : f)));
      },
      error: err => {
        this.busy.set(null);
        this.lastPull.set(null);
        this.snackBar.open(errorMessage(err, `Pulling ${feed.name} failed`), 'Close', { duration: 6000 });
        this.load();  // the feed records the failed pull
      },
    });
  }
}
