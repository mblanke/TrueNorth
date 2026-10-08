// helpers/ws.js — The API's event socket, /ws/{channel} (control-plane/api/app/main.py).
// A channel of the user's tenant: "tenant.<id>" (app/ws_auth.py). With a token, it is
// the second subprotocol ("bearer", token); an AUTH_DISABLED stack needs none. The
// server answers each JSON frame it has no action for with {"type": "ack", "data": <frame>},
// so a frame carrying its send time measures the round trip; it pings, and drops a
// socket that does not answer {"type": "pong"}.
import ws from "k6/ws";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import { API_PREFIX, AUTH_TOKEN, WS_URL } from "../config.js";

export const wsConnectDuration = new Trend("ws_connect_duration", true);
export const wsMessageLatency = new Trend("ws_message_latency", true);
export const wsMessages = new Counter("ws_messages_received");
export const wsErrors = new Rate("ws_errors");

/** Hold a socket on `channel` for `holdMs`, sending a timed frame every `everyMs`. */
export function wsSession(channel, holdMs, everyMs) {
  const params = AUTH_TOKEN ? { headers: { "Sec-WebSocket-Protocol": `bearer, ${AUTH_TOKEN}` } } : {};
  const started = Date.now();
  let acked = 0;
  const res = ws.connect(`${WS_URL}${API_PREFIX}/ws/${channel}`, params, (socket) => {
    socket.on("open", () => {
      wsConnectDuration.add(Date.now() - started);
      socket.setInterval(() => socket.send(JSON.stringify({ type: "latency", ts: Date.now() })), everyMs || 1000);
      socket.setTimeout(() => socket.close(), holdMs);
    });
    socket.on("message", (raw) => {
      wsMessages.add(1);
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch (_) {
        return;
      }
      if (msg.type === "ping") {
        socket.send(JSON.stringify({ type: "pong" }));
      } else if (msg.type === "ack") {
        try {
          const sent = JSON.parse(msg.data).ts;
          if (sent) {
            wsMessageLatency.add(Date.now() - sent);
            acked += 1;
          }
        } catch (_) {
          // an ack of a frame this session did not time
        }
      }
    });
    socket.on("error", () => wsErrors.add(true));
  });
  const ok = check(res, { "ws upgraded (101)": (r) => r && r.status === 101 }) && check(acked, { "ws frames acked": (n) => n > 0 });
  wsErrors.add(!ok);
  return ok;
}
