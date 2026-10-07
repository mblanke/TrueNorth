/**
 * A readable message from an API error, for toasts in the Wiki and Support pages.
 *
 * FastAPI sends `detail` as a string for most refusals, but as a list of
 * `{loc, msg}` objects for validation errors (422); passing that list straight to a
 * toast showed "[object Object]".
 */
export function apiErrorMessage(err: unknown, fallback: string): string {
  const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map(d => {
        const item = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(item.loc) ? String(item.loc[item.loc.length - 1] ?? '') : '';
        const msg = (item.msg ?? '').replace(/^Value error, /, '');
        return field && field !== 'body' ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean)
      .join('; ') || fallback;
  }
  return fallback;
}
