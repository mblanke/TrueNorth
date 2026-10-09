import { Injectable, computed, inject, signal } from '@angular/core';
import { Arc2StudioApiService, Arc2Status } from '@core/services/arc2-studio-api.service';

/**
 * Whether ARC² Course Studio is enabled on this server (GET /arc2/status), shared by the
 * Authoring hub's tab and the Studio page so a server with it off shows no menu entry and
 * no failing calls. Presentation only: the API refuses /arc2/runs on its own when off.
 */
@Injectable({ providedIn: 'root' })
export class Arc2AvailabilityService {
  private readonly api = inject(Arc2StudioApiService);
  private inFlight = false;

  /** Null until the server answers. */
  readonly status = signal<Arc2Status | null>(null);
  /** Only true once the server says so; an unanswered or failed check keeps the tab hidden. */
  readonly enabled = computed(() => this.status()?.enabled === true);

  /** Ask the server once per session; `force` asks again (the Studio page does, on open). */
  load(force = false, done?: (s: Arc2Status | null) => void): void {
    if (!force && (this.status() || this.inFlight)) { done?.(this.status()); return; }
    this.inFlight = true;
    this.api.status().subscribe({
      next: s => { this.inFlight = false; this.status.set(s); done?.(s); },
      error: () => { this.inFlight = false; done?.(null); },
    });
  }
}
