import { InjectionToken } from '@angular/core';

/** What the viewer tells the component. */
export interface RfbEvents {
  /** The remote screen is up. */
  connected(): void;
  /** The socket closed; `code` and `reason` are the relay's (1006 when the browser gives none). */
  closed(code: number, reason: string): void;
}

export interface RfbConnectOptions {
  /** The element the remote screen is drawn into. */
  target: HTMLElement;
  /** TrueNorth's relay, `wss://<tn>/ws/console/<grant>`. */
  url: string;
  /** The signed-in token, sent as the `bearer` subprotocol; empty when sign-in is off. */
  token: string;
  /** Send no keyboard or pointer input. */
  viewOnly: boolean;
}

/**
 * The RFB viewer behind a small interface, so the choice between noVNC and wmks.js (ADR 0014,
 * spike S2 on a real ESXi 8) stays open: the component knows nothing of either.
 */
export interface RfbClient {
  connect(options: RfbConnectOptions, events: RfbEvents): void;
  disconnect(): void;
  sendCtrlAltDel(): void;
}

/** Makes a viewer; async so the library behind it can be loaded only when a console opens. */
export type RfbClientFactory = () => Promise<RfbClient>;

/** The default is noVNC, loaded on first use. A test or another viewer provides its own. */
export const RFB_CLIENT_FACTORY = new InjectionToken<RfbClientFactory>('RFB_CLIENT_FACTORY', {
  providedIn: 'root',
  factory: () => async () => (await import('./novnc-rfb-client')).createNovncClient(),
});

/** The subprotocols for the relay: `["bearer", token]`, as `ws_auth` expects. */
export function relayProtocols(token: string): string[] {
  return token ? ['bearer', token] : [];
}
