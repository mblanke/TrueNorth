import { isAppPath, renderMarkdown } from './markdown';

/** Wiki pages and ticket text are written by users and read by everyone in the tenant. */
describe('renderMarkdown', () => {
  it('renders ordinary markdown', () => {
    const html = renderMarkdown('# Title\n\nSome **bold** and `code`.\n\n- one\n- two');
    expect(html).toContain('<h1');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<code>code</code>');
    expect(html).toContain('<li>two</li>');
  });

  it('strips script tags and inline event handlers', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\n<img src="x" onerror="alert(2)">\n\n<a href="#" onclick="alert(3)">x</a>');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('onclick');
  });

  it('drops javascript: links and embedded frames', () => {
    const html = renderMarkdown('[click](javascript:alert(1))\n\n<iframe src="https://evil.example"></iframe>');
    expect(html).not.toContain('javascript:');
    expect(html).not.toContain('<iframe');
  });

  it('opens external links in a new tab without an opener', () => {
    const html = renderMarkdown('[docs](https://example.com)');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('keeps in-app links in the same tab', () => {
    expect(renderMarkdown('[wiki](/wiki/handbook)')).not.toContain('target=');
  });

  it('strips style, class and id so text cannot overlay the app or pose as a staff note', () => {
    const html = renderMarkdown(
      '<div style="position:fixed;inset:0;z-index:9999">Session expired</div>\n\n' +
      '<span class="tn-kb-tag" id="x">Internal note</span>',
    );
    expect(html).not.toContain('style=');
    expect(html).not.toContain('class=');
    expect(html).not.toContain('id=');
  });

  it('treats /\\host and //host as off-site links', () => {
    expect(isAppPath('/wiki/a')).toBeTrue();
    expect(isAppPath('//evil.example')).toBeFalse();
    expect(isAppPath('/\\evil.example')).toBeFalse();
    // Markdown links percent-encode the backslash; a raw <a> tag keeps it.
    expect(renderMarkdown('<a href="/\\evil.example">x</a>')).toContain('rel="noopener noreferrer"');
  });

  it('tolerates empty input', () => {
    expect(renderMarkdown(null)).toBe('');
    expect(renderMarkdown(undefined)).toBe('');
  });
});
