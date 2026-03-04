// helpers/checks.js — Reusable k6 check functions for TrueNorth Range
import { check } from "k6";

/**
 * Verify HTTP status code matches expected value.
 * @param {object} res       - k6 HTTP response
 * @param {number} expected  - expected status code (default 200)
 * @returns {boolean}
 */
export function checkStatus(res, expected) {
  expected = expected || 200;
  return check(res, {
    [`status is ${expected}`]: (r) => r.status === expected,
  });
}

/**
 * Verify response time is within threshold.
 * @param {object} res        - k6 HTTP response
 * @param {number} threshold  - maximum acceptable duration in ms (default 500)
 * @returns {boolean}
 */
export function checkResponseTime(res, threshold) {
  threshold = threshold || 500;
  return check(res, {
    [`response time < ${threshold}ms`]: (r) => r.timings.duration < threshold,
  });
}

/**
 * Verify a value at a JSON path equals the expected value.
 * Supports dot-notation paths: "data.items[0].name"
 * @param {object} res       - k6 HTTP response
 * @param {string} path      - dot-notation path into the JSON body
 * @param {*}      expected  - expected value (strict equality)
 * @returns {boolean}
 */
export function checkJsonPath(res, path, expected) {
  return check(res, {
    [`json ${path} === ${JSON.stringify(expected)}`]: (r) => {
      try {
        const body = r.json();
        const value = path.split(".").reduce((obj, key) => {
          // handle array notation like "items[0]"
          const match = key.match(/^(\w+)\[(\d+)\]$/);
          if (match) {
            return obj[match[1]][parseInt(match[2], 10)];
          }
          return obj[key];
        }, body);
        return value === expected;
      } catch (_) {
        return false;
      }
    },
  });
}

/**
 * Verify the response body is valid JSON.
 * @param {object} res - k6 HTTP response
 * @returns {boolean}
 */
export function checkIsJson(res) {
  return check(res, {
    "response is valid JSON": (r) => {
      try { r.json(); return true; } catch (_) { return false; }
    },
  });
}

/**
 * Verify the response body contains a specific key.
 * @param {object} res - k6 HTTP response
 * @param {string} key - top-level key to check
 * @returns {boolean}
 */
export function checkHasKey(res, key) {
  return check(res, {
    [`response has key '${key}'`]: (r) => {
      try { return r.json().hasOwnProperty(key); } catch (_) { return false; }
    },
  });
}

/**
 * Compound check: status + response time + valid JSON.
 * @param {object} res          - k6 HTTP response
 * @param {number} status       - expected status
 * @param {number} maxDuration  - max acceptable ms
 * @returns {boolean}
 */
export function checkAll(res, status, maxDuration) {
  return (
    checkStatus(res, status) &&
    checkResponseTime(res, maxDuration) &&
    checkIsJson(res)
  );
}