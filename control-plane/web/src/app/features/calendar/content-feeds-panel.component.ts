import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ContentReviewApiService, FeedSource, FeedStatus } from '@core/services/content-review-api.service';
import { NotificationService } from '@core/services/notification.service';
import { apiError } from '../ranges/range-capabilities';

const MODES: Record<string, string> = { off: 'Off', online: 'Online', bundle: 'Signed bundles' };

/**
 * The Content view's Feeds panel (ADR 0014 decision 25, issue #287), for admins: whether the
 * release and vulnerability feeds are off, pulled online daily, or imported as signed
 * bundles; each source's last pull or import and its error; and, on an air-gapped site,
 * Import bundle. Feeds only flag content: the flags appear on the items at the content
 * clock's next sync.
 */
@Component({
  selector: 'tn-content-feeds-panel',
  imports: [DatePipe, MatButtonModule, MatIconModule],
  template: `
    <section class="card" aria-label="Feeds">
      <div class="card-head">
        <h2>Feeds</h2>
        @if (status(); as st) { <span class="mode" [class.on]="st.mode !== 'off'">{{ modeLabel(st.mode) }}</span> }
      </div>
      @if (status(); as st) {
        @if (st.mode === 'off') {
          <p class="muted">Release and vulnerability feeds are off. The installer turns them on
            (<code>tn_feeds_mode</code>).</p>
        }
        @for (s of st.sources; track s.source) {
          <div class="source">
            <mat-icon class="state" [class.bad]="!!s.last_error" aria-hidden="true">{{ icon(s) }}</mat-icon>
            <div class="what">
              <div class="label">{{ s.label }}</div>
              <div class="when">
                @if (s.last_success_at) { Last {{ s.source === 'bundle' ? 'import' : 'pull' }}
                  {{ s.last_success_at | date: 'd MMM y, HH:mm' }} } @else { Never }
                @if (s.entries) { · {{ s.entries }} entries }
              </div>
              @if (s.generated_at) {
                <div class="when">Made {{ s.generated_at | date: 'd MMM y' }} · key {{ s.key_id }}</div>
              }
              @if (s.last_error) { <div class="err" role="status">{{ s.last_error }}</div> }
            </div>
          </div>
        }
        <div class="foot">{{ st.entries }} entries · {{ st.flagged_items }} items flagged</div>
        @if (st.mode === 'bundle') {
          <input #picker type="file" accept=".json,application/json" hidden (change)="picked(picker)">
          <button mat-stroked-button (click)="picker.click()" [disabled]="busy() || !st.can_import">
            <mat-icon>upload_file</mat-icon> Import bundle
          </button>
          @if (!st.can_import && st.bundle_key_problem) { <p class="muted">{{ st.bundle_key_problem }}</p> }
          @if (!st.can_import && !st.bundle_key_problem) {
            <p class="muted">A platform administrator imports feed bundles.</p>
          }
        }
      } @else if (error()) {
        <p class="err" role="alert">{{ error() }}</p>
      } @else {
        <p class="muted">Loading…</p>
      }
    </section>
  `,
  styles: [`
    .card { background: var(--mat-sys-surface); border-radius: 18px; padding: 18px; display: flex; flex-direction: column;
      gap: 10px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .card-head { display: flex; align-items: center; justify-content: space-between; }
    .card-head h2 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-on-surface-variant); }
    .mode { border-radius: 10px; padding: 2px 10px; font-size: 12px; font-weight: 700;
      background: var(--mat-sys-surface-container-high); color: var(--mat-sys-on-surface-variant); }
    .mode.on { background: #ECFDF3; color: #067647; }
    .source { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 14px;
      background: var(--mat-sys-surface-container-low); }
    .state { color: #067647; font-size: 20px; width: 20px; height: 20px; flex: none; }
    .state.bad { color: #B42318; }
    .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .label { font-size: 13px; font-weight: 600; }
    .when { font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .err { font-size: 12px; color: #B42318; overflow-wrap: anywhere; }
    .foot { font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; margin: 0; }
    code { font-size: 12px; }
  `],
})
export class ContentFeedsPanelComponent implements OnInit {
  private readonly api = inject(ContentReviewApiService);
  private readonly notify = inject(NotificationService);

  readonly status = signal<FeedStatus | null>(null);
  readonly error = signal('');
  readonly busy = signal(false);

  ngOnInit(): void {
    this.load();
  }

  modeLabel(mode: string): string {
    return MODES[mode] ?? mode;
  }

  icon(s: FeedSource): string {
    if (s.last_error) return 'error';
    return s.last_success_at ? 'check_circle' : 'radio_button_unchecked';
  }

  load(): void {
    this.api.feedStatus().subscribe({
      next: st => { this.status.set(st); this.error.set(''); },
      error: e => this.error.set(apiError(e)),
    });
  }

  picked(input: HTMLInputElement): void {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    this.busy.set(true);
    this.api.importFeedBundle(file).subscribe({
      next: res => {
        this.busy.set(false);
        this.notify.success(`${res.entries} feed entries imported (${res.new} new); flags follow within the hour`);
        this.load();
      },
      error: e => {
        this.busy.set(false);
        this.notify.error(apiError(e));
        this.load();
      },
    });
  }
}
