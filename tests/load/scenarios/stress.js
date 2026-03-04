// scenarios/stress.js — Stress test: 1,200 users, 15 minutes
// Target: 1,200 concurrent users — validates system at max designed capacity.

import http from "k6/http";
import { check, group, sleep } from "k6";
import ws from "k6/ws";
import { BASE_URL, WS_URL, headers, stressThresholds, rangeCreationDuration, provisionDuration, wsMessageLatency } from "../config.js";
import { randomRangePayload, randomTemplatePayload, uuidv4 } from "../helpers/data.js";
import { checkStatus, checkIsJson } from "../helpers/checks.js";

export const options = {
  scenarios: {
    stress_ramp: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m",  target: 300  },
        { duration: "2m",  target: 600  },
        { duration: "3m",  target: 1200 },
        { duration: "5m",  target: 1200 },  // hold at peak
        { duration: "3m",  target: 0    },  // drain
      ],
      gracefulRampDown: "60s",
    },
  },
  thresholds: stressThresholds,
  tags: { testType: "stress" },
};

export default function () {
  const h = headers();
  const roll = Math.random();

  if (roll < 0.35) {
    // ── Range creation under heavy load ───────────────────────────────────
    group("Stress: Create Ranges", function () {
      const tplRes = http.post(`${BASE_URL}/templates`, randomTemplatePayload(), { headers: h });
      let templateId;
      try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

      const start = Date.now();
      const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      rangeCreationDuration.add(Date.now() - start);

      check(res, {
        "range created (2xx)": (r) => r.status >= 200 && r.status < 300,
      });

      let rangeId;
      try { rangeId = res.json().id; } catch (_) {}

      if (rangeId) {
        const pStart = Date.now();
        http.post(`${BASE_URL}/ranges/${rangeId}/provision`, null, { headers: h });
        provisionDuration.add(Date.now() - pStart);
      }
    });
  } else if (roll < 0.60) {
    // ── List ranges with pagination simulation ────────────────────────────
    group("Stress: List Ranges", function () {
      const pages = [0, 1, 2, 3, 4];
      for (const page of pages) {
        const res = http.get(`${BASE_URL}/ranges?page=${page}&per_page=50`, { headers: h });
        checkStatus(res, 200);
        checkIsJson(res);
      }
    });
  } else if (roll < 0.80) {
    // ── Stats endpoint hammering ──────────────────────────────────────────
    group("Stress: Range Stats", function () {
      for (let i = 0; i < 5; i++) {
        const res = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
        checkStatus(res, 200);
        sleep(0.2);
      }
    });
  } else {
    // ── WebSocket subscription burst ──────────────────────────────────────
    group("Stress: WebSocket", function () {
      const url = `${WS_URL}/ws/range`;
      const res = ws.connect(url, {}, function (socket) {
        socket.on("open", function () {
          const sendTime = Date.now();
          socket.send(JSON.stringify({
            type: "subscribe",
            channel: "range",
            filters: { tenant_id: uuidv4() },
          }));

          socket.on("message", function (msg) {
            wsMessageLatency.add(Date.now() - sendTime);
          });
        });

        socket.on("error", function (e) {
          check(null, { "ws no error": () => false });
        });

        socket.setTimeout(function () {
          socket.close();
        }, 5000);
      });

      check(res, {
        "ws connected": (r) => r && r.status === 101,
      });
    });
  }

  sleep(Math.random() * 1.5 + 0.3);
}