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
 * Images (and any other `src`) load only from this origin: a remote `<img>` is a beacon
 * that tells its author who read the page, when, and from which address. `srcset` and
 * `poster` are dropped for the same reason.
 */
let hooked = false;
/** Images whose src the viewer supplied (a blob URL it created); never anything from the page's own HTML. */
const trusted = new WeakSet<Element>();

/** A same-site path: `/wiki/x`, not `//evil.com` or `/\\evil.com` (browsers read both as off-site). */
export const isAppPath = (href: string): boolean => /^\/(?![/\\])/.test(href);

/** Whether `url` (absolute or relative) resolves to this app's own origin over http(s). */
export function isSameOrigin(url: string): boolean {
  if (!url.trim()) return false;
  try {
    const resolved = new URL(url, window.location.href);
    return /^https?:$/.test(resolved.protocol) && resolved.origin === window.location.origin;
  } catch {
    return false;
  }
}

/** A wiki image in a page: `/api/wiki/images/<id>`. It needs the user's token, so it is fetched, not linked. */
const WIKI_IMAGE = /^(?:https?:\/\/[^/]+)?\/api(?:\/v1)?\/wiki\/images\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;

/** Attribute that marks an <img> to be filled in with the fetched image (MarkdownViewComponent). */
export const WIKI_IMAGE_ATTR = 'data-wiki-image';

/**
 * Set only while `renderMarkdown` runs: turns a wiki image id into the blob URL the viewer already
 * fetched, so a re-render (every keystroke in the editor preview) keeps showing the picture instead
 * of blanking it until the next fetch tick. Only the viewer supplies it, and only with URLs it made.
 */
let knownImage: ((id: string) => string | undefined) | null = null;

function ensureHooks(): void {
  if (hooked) return;
  hooked = true;
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    node.removeAttribute(WIKI_IMAGE_ATTR); // only this hook may set it, never the page's own HTML
    if (node.tagName === 'IMG') {
      const id = WIKI_IMAGE.exec((node.getAttribute('src') ?? '').trim())?.[1];
      if (id) {
        node.setAttribute(WIKI_IMAGE_ATTR, id.toLowerCase());
        node.setAttribute('loading', 'lazy');
        const ready = knownImage?.(id.toLowerCase());
        if (ready) {
          node.setAttribute('src', ready);
          trusted.add(node);
        } else {
          node.removeAttribute('src'); // no unauthenticated request; the viewer fetches it
        }
      }
    }
    if (node.hasAttribute('src') && !trusted.has(node) && !isSameOrigin(node.getAttribute('src') ?? '')) {
      node.removeAttribute('src');
    }
    node.removeAttribute('srcset');
    node.removeAttribute('poster');
    if (node.tagName === 'A' && node.getAttribute('href')) {
      const href = node.getAttribute('href') ?? '';
      if (!href.startsWith('#') && !isAppPath(href)) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    }
  });
}

export function renderMarkdown(
  source: string | null | undefined,
  imageUrl?: (id: string) => string | undefined,
): string {
  ensureHooks();
  knownImage = imageUrl ?? null;
  try {
    return render(source);
  } finally {
    knownImage = null;
  }
}

function render(source: string | null | undefined): string {
  const html = marked.parse(source ?? '', { async: false, gfm: true, breaks: true }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'iframe', 'form', 'input', 'button', 'object', 'embed'],
    FORBID_ATTR: ['style', 'class', 'id'],
    ADD_ATTR: ['target'],
  });
}
