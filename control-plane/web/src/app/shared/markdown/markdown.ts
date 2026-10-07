import DOMPurify from 'dompurify';
import { marked } from 'marked';

/**
 * Markdown → safe HTML for wiki pages and ticket text.
 *
 * Everything here is user-written, and a wiki page is read by every student in the
 * tenant, so the output of `marked` is never trusted: DOMPurify strips scripts, event
 * handlers, `javascript:` URLs, iframes and forms before anything reaches the DOM.
 * `style`, `class` and `id` are stripped too: with them a ticket comment could cover
 * the whole app with a fixed-position fake sign-in screen, or borrow the app's own
 * classes to dress itself up as a staff "Internal note".
 * Links leave in a new tab with `rel="noopener noreferrer"` so a linked page cannot
 * reach back through `window.opener`.
 */
let hooked = false;

/** A same-site path: `/wiki/x`, not `//evil.com` or `/\\evil.com` (browsers read both as off-site). */
export const isAppPath = (href: string): boolean => /^\/(?![/\\])/.test(href);

function ensureHooks(): void {
  if (hooked) return;
  hooked = true;
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.tagName === 'A' && node.getAttribute('href')) {
      const href = node.getAttribute('href') ?? '';
      if (!href.startsWith('#') && !isAppPath(href)) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    }
  });
}

export function renderMarkdown(source: string | null | undefined): string {
  ensureHooks();
  const html = marked.parse(source ?? '', { async: false, gfm: true, breaks: true }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'button', 'object', 'embed'],
    FORBID_ATTR: ['style', 'class', 'id'],
    ADD_ATTR: ['target'],
  });
}
