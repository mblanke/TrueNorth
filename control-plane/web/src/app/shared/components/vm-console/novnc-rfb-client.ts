import RFB from '@novnc/novnc/lib/rfb';

import { RfbClient, RfbConnectOptions, RfbEvents, relayProtocols } from './rfb-client';

/**
 * noVNC (MPL-2.0) as the RFB viewer. Loaded lazily by `RFB_CLIENT_FACTORY`, so the library is
 * a separate chunk fetched when a Student opens a console. Whether VMware's RFB dialect works
 * with it is what spike S2 finds out on a real ESXi 8; if it does not, another `RfbClient`
 * (wmks.js) replaces this one without touching the component.
 *
 * The socket is made here and handed to noVNC as its channel, because noVNC's own
 * `disconnect` event carries no close code and the relay's codes are what the Student is told.
 */
class NovncRfbClient implements RfbClient {
  private rfb: RFB | null = null;
  private socket: WebSocket | null = null;

  connect(options: RfbConnectOptions, events: RfbEvents): void {
    const socket = new WebSocket(options.url, relayProtocols(options.token));
    this.socket = socket;
    // addEventListener: noVNC takes over the onclose handler of a channel it is given.
    socket.addEventListener('close', e => events.closed(e.code, e.reason));
    const rfb = new RFB(options.target, socket);
    rfb.viewOnly = options.viewOnly;
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.addEventListener('connect', () => events.connected());
    this.rfb = rfb;
  }

  disconnect(): void {
    const { rfb, socket } = this;
    this.rfb = null;
    this.socket = null;
    try { rfb?.disconnect(); } catch { /* already closed */ }
    try { socket?.close(); } catch { /* already closed */ }
  }

  sendCtrlAltDel(): void {
    this.rfb?.sendCtrlAltDel();
  }
}

export function createNovncClient(): RfbClient {
  return new NovncRfbClient();
}
