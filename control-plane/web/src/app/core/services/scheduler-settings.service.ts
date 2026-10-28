import { Injectable, computed, inject, signal } from '@angular/core';
import { SchedulerApiService, SchedulerSettings } from './scheduler-api.service';

/**
 * The site's scheduler settings (GET /schedule/settings), asked once per session and shared
 * by the pages that say them in words: "ranges are built 30 minutes before", "none free in
 * the next 14 days". The site sets them (SCHEDULER_PROVISION_LEAD_MIN,
 * SCHEDULER_NEXT_FREE_DAYS); the pages used to hard-code the defaults (#318). Until the
 * server answers, or for a caller who may not read the schedule, the phrases leave the
 * number out rather than guess it.
 */
@Injectable({ providedIn: 'root' })
export class SchedulerSettingsService {
  private readonly api = inject(SchedulerApiService);
  private inFlight = false;

  readonly settings = signal<SchedulerSettings | null>(null);

  /** "30 minutes before", or "before" while unknown. */
  readonly leadPhrase = computed(() => {
    const m = this.settings()?.lead_minutes;
    return m === undefined ? 'before' : `${durationWords(m)} before`;
  });

  /** "in the next 14 days", or "soon" while unknown. */
  readonly nextFreeHorizon = computed(() => {
    const d = this.settings()?.next_free_days;
    return d === undefined ? 'soon' : `in the next ${d} day${d === 1 ? '' : 's'}`;
  });

  load(): void {
    if (this.settings() || this.inFlight) return;
    this.inFlight = true;
    this.api.settings().subscribe({
      next: s => { this.inFlight = false; this.settings.set(s); },
      error: () => { this.inFlight = false; },
    });
  }
}

/** 30 -> "30 minutes", 60 -> "1 hour", 90 -> "90 minutes", 120 -> "2 hours". */
export function durationWords(minutes: number): string {
  if (minutes > 0 && minutes % 60 === 0) {
    const h = minutes / 60;
    return `${h} hour${h === 1 ? '' : 's'}`;
  }
  return `${minutes} minute${minutes === 1 ? '' : 's'}`;
}
