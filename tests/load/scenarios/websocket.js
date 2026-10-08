// scenarios/websocket.js — Event sockets: 500 concurrent connections on the user's tenant
// channel, round-trip latency of timed frames, and a reconnect per iteration.

import { sleep } from "k6";
import { Counter } from "k6/metrics";
import { thresholds } from "../config.js";
import { tenantId } from "../helpers/flows.js";
import { wsSession } from "../helpers/ws.js";

const wsReconnects = new Counter("ws_reconnects");

const WS_THRESHOLDS = {
  ws_connect_duration: ["p(95)<2000"],
  ws_message_latency: ["p(95)<500"],
};

export const options = {
  scenarios: {
    websocket_load: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 100 },
        { duration: "1m", target: 300 },
        { duration: "1m", target: 500 },
        { duration: "3m", target: 500 }, // hold
        { duration: "1m", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: thresholds(
    Object.assign({ ws_errors: ["rate<0.05"] }, WS_THRESHOLDS),
    Object.assign({ ws_errors: ["rate<0.01"] }, WS_THRESHOLDS),
  ),
  tags: { testType: "websocket" },
};

export function setup() {
  return { channel: `tenant.${tenantId()}` };
}

export default function (fx) {
  // Hold for 10–20 s, then reconnect once, as a client does after a drop.
  if (wsSession(fx.channel, 10000 + Math.floor(Math.random() * 10000), 3000)) {
    sleep(1);
    wsReconnects.add(1);
    wsSession(fx.channel, 10000 + Math.floor(Math.random() * 10000), 3000);
  }
  sleep(Math.random() * 2 + 1);
}
