// scenarios/websocket.js — WebSocket load test
// 500 concurrent connections, subscribe/message latency, reconnection behavior

import ws from "k6/ws";
import { check, sleep } from "k6";
import { Trend, Counter, Rate } from "k6/metrics";
import { WS_URL } from "../config.js";
import { uuidv4 } from "../helpers/data.js";

const wsConnectDuration = new Trend("ws_connect_duration", true);
const wsMsgLatency      = new Trend("ws_message_latency",  true);
const wsMessages        = new Counter("ws_messages_received");
const wsErrors          = new Rate("ws_errors");
const wsReconnects      = new Counter("ws_reconnects");

export const options = {
  scenarios: {
    websocket_load: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m",  target: 100 },
        { duration: "1m",  target: 300 },
        { duration: "1m",  target: 500 },
        { duration: "3m",  target: 500 },  // hold
        { duration: "1m",  target: 0   },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: {
    ws_connect_duration: ["p(95)<2000"],
    ws_message_latency:  ["p(95)<500"],
    ws_errors:           ["rate<0.05"],
  },
  tags: { testType: "websocket" },
};

// Channels to rotate through
const channels = ["range", "exercise", "provision", "telemetry", "system"];

function connectAndSubscribe(channel) {
  const url = `${WS_URL}/ws/${channel}`;
  const connectStart = Date.now();

  const res = ws.connect(url, {}, function (socket) {
    wsConnectDuration.add(Date.now() - connectStart);

    socket.on("open", function () {
      // Subscribe message
      const subPayload = JSON.stringify({
        type: "subscribe",
        channel: channel,
        client_id: uuidv4(),
        filters: {},
      });
      socket.send(subPayload);

      // Periodically send pings to keep connection alive
      socket.setInterval(function () {
        const pingTime = Date.now();
        socket.send(JSON.stringify({ type: "ping", ts: pingTime }));
      }, 3000);
    });

    socket.on("message", function (msg) {
      wsMessages.add(1);
      try {
        const data = JSON.parse(msg);
        if (data.ts) {
          wsMsgLatency.add(Date.now() - data.ts);
        }
      } catch (_) {
        // Non-JSON message, just count it
      }
    });

    socket.on("error", function (e) {
      wsErrors.add(true);
    });

    socket.on("close", function () {
      // no-op
    });

    // Hold connection open for 10–20 seconds
    const holdTime = 10000 + Math.floor(Math.random() * 10000);
    socket.setTimeout(function () {
      socket.close();
    }, holdTime);
  });

  const connected = check(res, {
    "ws status 101 (switching protocols)": (r) => r && r.status === 101,
  });

  if (!connected) {
    wsErrors.add(true);
  }

  return connected;
}

export default function () {
  const channel = channels[Math.floor(Math.random() * channels.length)];

  // First connection
  let connected = connectAndSubscribe(channel);

  // Simulate reconnection: if first connection succeeded, reconnect once
  if (connected) {
    sleep(1);
    wsReconnects.add(1);
    connectAndSubscribe(channel);
  }

  sleep(Math.random() * 2 + 1);
}