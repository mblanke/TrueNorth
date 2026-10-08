/**
 * Validate a `returnUrl` query parameter before navigating to it after sign-in.
 *
 * Only same-origin, app-relative paths are accepted: they must start with a single
 * `/`. Absolute URLs (`https://evil.test`), protocol-relative URLs (`//evil.test`),
 * backslash variants that browsers normalise to `//` (`/\evil.test`), scheme tricks
 * (`javascript:`), and control characters are all rejected, so the parameter cannot
 * be used as an open redirect. A return to `/login` itself is rejected to avoid a loop.
 *
 * Returns the safe path, or `null` when the value must be ignored.
 */
export function safeReturnUrl(raw: string | null | undefined, origin = window.location.origin): string | null {
  if (!raw || typeof raw !== 'string') return null;
  // Control characters (incl. tab/newline, which URL parsing strips) and backslashes.
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i);
    if (code < 0x20 || code === 0x7f || raw[i] === '\\') return null;
  }
  if (!raw.startsWith('/') || raw.startsWith('//')) return null;

  let parsed: URL;
  try {
    parsed = new URL(raw, origin);
  } catch {
    return null;
  }
  if (parsed.origin !== new URL(origin).origin) return null;

  const path = `${parsed.pathname}${parsed.search}${parsed.hash}`;
  if (parsed.pathname === '/login' || parsed.pathname.startsWith('/login/')) return null;
  return path;
}
