import type { components } from '@core/api/schema';

type S = components['schemas'];

/**
 * What a console grant gives the viewer: where to connect and for how long the link is good.
 * Both the class consoles (`ClassConsoleOut`) and the lab console (`ConsoleOut`) fit it.
 */
export type ConsoleGrant = Pick<S['ClassConsoleOut'], 'url' | 'expires_in'>
  & Partial<Pick<S['ClassConsoleOut'], 'view_only' | 'observed'>>;

/** The state line: idle before the first request, then the four states the Student sees. */
export type ConsoleStatus = 'idle' | 'connecting' | 'connected' | 'ended' | 'closed';

/** Words for a finished console. `detail` is the relay's own reason, which is safe to show. */
export interface ConsoleNotice {
  status: 'ended' | 'closed';
  text: string;
  detail: string;
}

const ENDED = (text: string): [ConsoleNotice['status'], string] => ['ended', text];
const CLOSED = (text: string): [ConsoleNotice['status'], string] => ['closed', text];

/** WebSocket close codes of `/ws/console/{grant}` (console_relay/router.py) in the Student's words. */
const CLOSE_TEXT: Record<number, [ConsoleNotice['status'], string]> = {
  1000: ENDED('The console was closed.'),
  1001: ENDED('This console has been open a long time. Press Reconnect to carry on.'),
  1006: CLOSED('TrueNorth could not open the console, or the connection was lost. Press Reconnect.'),
  1008: CLOSED('You cannot open this console now: your session window may have closed or your access changed.'),
  1009: CLOSED('The console was closed because your browser sent a message that was too large.'),
  1011: CLOSED('The console service had a problem. Press Reconnect.'),
  4404: CLOSED('The machine is not running or is gone. Wait for the range, then press Reconnect.'),
  4408: ENDED('The console was closed after sitting idle. Press Reconnect to open it again.'),
  4409: CLOSED('This console is already open in another window.'),
  4410: ENDED('This console link expired. Press Reconnect for a new one.'),
  4429: CLOSED('Too many attempts. Wait a minute, then press Reconnect.'),
  4502: CLOSED('The hypervisor did not give a console. Tell your instructor; this is logged.'),
  4503: CLOSED('The connection to the machine was lost. Press Reconnect.'),
  4504: CLOSED('The hypervisor did not answer in time. Press Reconnect.'),
};

/** Maps a close code and reason from the relay (or the browser) to a state and words. */
export function closeNotice(code: number, reason = ''): ConsoleNotice {
  const [status, text] = CLOSE_TEXT[code] ?? CLOSED(`The console closed (code ${code}). Press Reconnect.`);
  const detail = reason.trim();
  return { status, text, detail: detail === text ? '' : detail };
}

/** The words for a link that ran out before the viewer connected. */
export function expiredNotice(): ConsoleNotice {
  return closeNotice(4410);
}

/** Words for a failed request for a console (the endpoints that issue the grant). */
export function openErrorText(err: unknown): string {
  const status = typeof err === 'object' && err !== null ? (err as { status?: unknown }).status : undefined;
  switch (status) {
    case 403:
      return 'Outside your session window: the console opens shortly before the session starts and closes when it ends.';
    case 409:
      return 'The machine is not running. Wait for the range to finish building, then press Reconnect.';
    case 429:
      return 'Too many attempts. Wait a minute, then press Reconnect.';
    case 502:
      return 'The hypervisor refused the console. Tell your instructor; this is logged.';
    case 0:
      return 'TrueNorth could not be reached. Check your connection, then press Reconnect.';
    default:
      return typeof status === 'number' ? `The console could not be opened (the API answered ${status}).`
        : 'The console could not be opened.';
  }
}
