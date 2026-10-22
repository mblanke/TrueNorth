import { ChangeDetectionStrategy, Component, OnInit, inject, input, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import {
  MaintenanceWindow, MaintenanceWindowsApiService,
} from '@core/services/maintenance-windows-api.service';

/**
 * Says so while a maintenance window is in effect (ADR 0013 §7): what is paused, why and
 * until when. Shown on the Ranges and Schedule pages. Nothing when no window is in effect,
 * or when the windows cannot be read (the API still refuses; this is only a heads-up).
 */
@Component({
  selector: 'tn-maintenance-banner',
  imports: [DatePipe, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @for (w of windows(); track w.id) {
      <div class="banner" role="status">
        <mat-icon aria-hidden="true">construction</mat-icon>
        <div>
          <strong>Maintenance window until {{ w.ends_at | date:'medium' }}{{ w.platform_wide ? ' (all organisations)' : '' }}:</strong>
          {{ w.reason }}.
          <span class="muted">Provisioning, resets and destroys are paused{{ scopeNote(w) }}. Automated cleanup still runs.</span>
        </div>
      </div>
    }
  `,
  styles: [
    `
      :host { display: block; }
      .banner {
        display: flex; gap: 12px; align-items: flex-start;
        padding: 12px 16px; margin: 0 0 12px;
        border: 1px solid var(--warn, #b26a00); border-left-width: 4px; border-radius: 8px;
        background: var(--bg-card);
      }
      mat-icon { color: var(--warn, #b26a00); flex: none; }
      .muted { color: var(--text-muted, inherit); }
    `,
  ],
})
export class MaintenanceBannerComponent implements OnInit {
  private readonly api = inject(MaintenanceWindowsApiService);

  /** Only the windows that cover this range. */
  readonly rangeId = input<string | undefined>(undefined);
  readonly windows = signal<MaintenanceWindow[]>([]);

  ngOnInit(): void {
    this.api.active(this.rangeId()).subscribe({
      next: ws => this.windows.set(ws),
      error: () => this.windows.set([]),
    });
  }

  scopeNote(w: MaintenanceWindow): string {
    if (w.scope === 'ranges') return ` for ${w.range_ids.length || 'some'} range${w.range_ids.length === 1 ? '' : 's'}`;
    if (w.scope === 'tags') return ` for ranges tagged ${w.tags.join(', ')}`;
    return '';
  }
}
