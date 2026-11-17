// @novnc/novnc ships no types; only what novnc-rfb-client.ts uses.
declare module '@novnc/novnc/lib/rfb' {
  export default class RFB {
    constructor(target: HTMLElement, channel: WebSocket, options?: { shared?: boolean });
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;
    sendCtrlAltDel(): void;
    disconnect(): void;
    addEventListener(type: 'connect' | 'disconnect' | 'securityfailure', listener: (e: CustomEvent) => void): void;
  }
}
