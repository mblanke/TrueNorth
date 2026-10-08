import { safeReturnUrl } from './return-url';

describe('safeReturnUrl', () => {
  const origin = 'https://range.example.test';

  it('accepts same-origin relative paths, keeping query and hash', () => {
    expect(safeReturnUrl('/labs/abc', origin)).toBe('/labs/abc');
    expect(safeReturnUrl('/scoring?exercise=e1#top', origin)).toBe('/scoring?exercise=e1#top');
  });

  it('ignores missing values', () => {
    expect(safeReturnUrl(null, origin)).toBeNull();
    expect(safeReturnUrl(undefined, origin)).toBeNull();
    expect(safeReturnUrl('', origin)).toBeNull();
  });

  it('rejects absolute URLs, even to the same host', () => {
    expect(safeReturnUrl('https://evil.test/labs/abc', origin)).toBeNull();
    expect(safeReturnUrl('https://range.example.test/labs/abc', origin)).toBeNull();
    expect(safeReturnUrl('javascript:alert(1)', origin)).toBeNull();
    expect(safeReturnUrl('labs/abc', origin)).toBeNull();
  });

  it('rejects protocol-relative and backslash variants', () => {
    expect(safeReturnUrl('//evil.test', origin)).toBeNull();
    expect(safeReturnUrl('/\\evil.test', origin)).toBeNull();
    expect(safeReturnUrl('\\\\evil.test', origin)).toBeNull();
  });

  it('rejects control characters that URL parsing would silently strip', () => {
    expect(safeReturnUrl('/\t/evil.test', origin)).toBeNull();
    expect(safeReturnUrl('/\n/evil.test', origin)).toBeNull();
  });

  it('rejects a return to the login page itself', () => {
    expect(safeReturnUrl('/login', origin)).toBeNull();
    expect(safeReturnUrl('/login?returnUrl=/labs/x', origin)).toBeNull();
  });
});
