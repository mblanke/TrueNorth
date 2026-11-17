import { Provider } from '@angular/core';

import { RFB_CLIENT_FACTORY, RfbClient, RfbConnectOptions, RfbEvents } from './rfb-client';

/**
 * A viewer for specs: records what the component asked and lets the spec play the relay's
 * part (`fake.up()`, `fake.close(4404, ...)`). Never used outside tests.
 */
export class FakeRfbClient implements RfbClient {
  options: RfbConnectOptions | null = null;
  events: RfbEvents | null = null;
  disconnects = 0;
  ctrlAltDels = 0;

  connect(options: RfbConnectOptions, events: RfbEvents): void {
    this.options = options;
    this.events = events;
  }

  disconnect(): void {
    this.disconnects += 1;
  }

  sendCtrlAltDel(): void {
    this.ctrlAltDels += 1;
  }

  /** The remote screen came up. */
  up(): void {
    this.events?.connected();
  }

  /** The relay closed the socket. */
  close(code: number, reason = ''): void {
    this.events?.closed(code, reason);
  }
}

/** Provides `RFB_CLIENT_FACTORY` that hands out fakes; `made` lists them in order. */
export function provideFakeRfb(made: FakeRfbClient[] = []): Provider {
  return {
    provide: RFB_CLIENT_FACTORY,
    useValue: () => {
      const fake = new FakeRfbClient();
      made.push(fake);
      return Promise.resolve(fake);
    },
  };
}
