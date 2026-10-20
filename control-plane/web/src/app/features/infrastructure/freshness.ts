import { ConnectionDiscovery, HypervisorConnection } from '@core/models';

/** How often the page re-reads the stored inventory while it is visible. The API itself
 *  rediscovers the hypervisors every INFRA_DISCOVERY_SECONDS (default 5 min). */
export const REREAD_MS = 60_000;

/** "just now", "4 min ago", "2 h 5 min ago", "3 d ago"; "never" for no time at all. */
export function updatedAgo(iso: string | null | undefined, now: Date): string {
  if (!iso) return 'never';
  const mins = Math.max(0, Math.floor((now.getTime() - new Date(iso).getTime()) / 60_000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  if (h < 48) return `${h} h ${mins % 60} min ago`;
  return `${Math.floor(h / 24)} d ago`;
}

/** "every 5 min", "every 90 s"; '' when the schedule is off. */
export function cadence(seconds: number | null | undefined): string {
  if (!seconds) return '';
  return seconds % 60 === 0 ? `every ${seconds / 60} min` : `every ${seconds} s`;
}

/**
 * The page's overall age: the oldest last success among the active connections, since the
 * figures are only as fresh as their stalest source. null when an active connection has
 * never been discovered (or there are none).
 */
export function overallUpdated(
  connections: readonly HypervisorConnection[],
  rows: readonly ConnectionDiscovery[],
): string | null {
  const byId = new Map(rows.map(r => [r.connection_id, r]));
  const active = connections.filter(c => c.is_active);
  if (!active.length) return null;
  let oldest: string | null = null;
  for (const c of active) {
    const at = byId.get(c.id)?.last_discovered_at;
    if (!at) return null;
    if (oldest === null || new Date(at).getTime() < new Date(oldest).getTime()) oldest = at;
  }
  return oldest;
}
