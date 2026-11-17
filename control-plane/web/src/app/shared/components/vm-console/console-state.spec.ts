import { closeNotice, expiredNotice, openErrorText } from './console-state';
import { relayProtocols } from './rfb-client';

describe('closeNotice', () => {
  const cases: [number, 'ended' | 'closed', string][] = [
    [1000, 'ended', 'closed'],
    [1001, 'ended', 'open a long time'],
    [1006, 'closed', 'could not open the console'],
    [1008, 'closed', 'session window'],
    [1009, 'closed', 'too large'],
    [1011, 'closed', 'service had a problem'],
    [4404, 'closed', 'not running'],
    [4408, 'ended', 'idle'],
    [4409, 'closed', 'another window'],
    [4410, 'ended', 'expired'],
    [4429, 'closed', 'Too many'],
    [4502, 'closed', 'did not give a console'],
    [4503, 'closed', 'connection to the machine was lost'],
    [4504, 'closed', 'did not answer'],
  ];
  for (const [code, status, words] of cases) {
    it(`maps ${code} to ${status} in words`, () => {
      const n = closeNotice(code);
      expect(n.status).toBe(status);
      expect(n.text).toContain(words);
    });
  }

  it('keeps the relay\'s reason as detail, and drops it when it repeats the text', () => {
    expect(closeNotice(1008, 'Your session has ended').detail).toBe('Your session has ended');
    const text = closeNotice(1008).text;
    expect(closeNotice(1008, text).detail).toBe('');
  });

  it('an unknown code is closed and names the code', () => {
    const n = closeNotice(4999);
    expect(n.status).toBe('closed');
    expect(n.text).toContain('4999');
  });

  it('a link that ran out is the expired notice', () => {
    expect(expiredNotice()).toEqual(closeNotice(4410));
  });
});

describe('openErrorText', () => {
  it('403 is outside the session window', () => {
    expect(openErrorText({ status: 403 })).toContain('Outside your session window');
  });
  it('409 is the machine not running', () => {
    expect(openErrorText({ status: 409 })).toContain('not running');
  });
  it('502 is the hypervisor refusing', () => {
    expect(openErrorText({ status: 502 })).toContain('hypervisor refused');
  });
  it('429, a dead network and other statuses have words too', () => {
    expect(openErrorText({ status: 429 })).toContain('Too many');
    expect(openErrorText({ status: 0 })).toContain('could not be reached');
    expect(openErrorText({ status: 500 })).toContain('500');
    expect(openErrorText(new Error('x'))).toBe('The console could not be opened.');
  });
});

describe('relayProtocols', () => {
  it('sends the token as the bearer subprotocol, and nothing without one', () => {
    expect(relayProtocols('abc')).toEqual(['bearer', 'abc']);
    expect(relayProtocols('')).toEqual([]);
  });
});
