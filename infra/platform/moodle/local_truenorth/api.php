<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * Server-to-server endpoint TrueNorth uses to push courses into this Moodle.
 *
 * Authentication is a TrueNorth-signed ticket (`typ` = "sync") in the Authorization
 * header, bound to the exact request body by its SHA-256 (`body_sha256`). No Moodle
 * web-service token exists for TrueNorth to store or leak.
 *
 * Request:  POST, JSON {"op": "upsert_course", "course": {...}}
 *                      or {"op": "hide_course" | "describe_course" | "delete_stage", "idnumber": "..."}
 *                      or {"op": "set_visible", "idnumber": "...", "visible": true|false}
 *                      or {"op": "pull_results", "cursor": "...", "limit": 500, "settle": 5}
 * Response: JSON {"ok": true, ...} or {"ok": false, "error": "..."} with a 4xx/5xx status.
 *           pull_results answers {"ok": true, "signed": "<JWT>"}: the results, signed with
 *           this site's LTI key and naming this ticket's jti (classes/results.php).
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

define('NO_MOODLE_COOKIES', true);
define('AJAX_SCRIPT', true);

require(__DIR__ . '/../../config.php');

/**
 * Send a JSON response and stop.
 *
 * @param int $status HTTP status
 * @param array $body
 */
function local_truenorth_reply(int $status, array $body): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($body);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    local_truenorth_reply(405, ['ok' => false, 'error' => 'POST only']);
}
$auth = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
if (!preg_match('/^Bearer\s+(\S+)$/', $auth, $m)) {
    local_truenorth_reply(401, ['ok' => false, 'error' => 'missing ticket']);
}
$raw = file_get_contents('php://input');

try {
    $claims = \local_truenorth\ticket::verify($m[1], 'sync');
} catch (\Throwable $e) {
    local_truenorth_reply(401, ['ok' => false, 'error' => 'ticket refused']);
}
if (!hash_equals((string) ($claims->body_sha256 ?? ''), hash('sha256', $raw))) {
    local_truenorth_reply(401, ['ok' => false, 'error' => 'ticket does not match body']);
}

$request = json_decode($raw, true);
if (!is_array($request) || !isset($request['op'])) {
    local_truenorth_reply(400, ['ok' => false, 'error' => 'bad request']);
}

// Moodle's course and module APIs check capabilities against the current user.
\core\session\manager::set_user(get_admin());

try {
    switch ($request['op']) {
        case 'upsert_course':
            $result = \local_truenorth\course_sync::upsert($request['course'] ?? []);
            break;
        case 'hide_course':
            $result = \local_truenorth\course_sync::hide((string) ($request['idnumber'] ?? ''));
            break;
        case 'describe_course':
            $result = \local_truenorth\course_sync::describe((string) ($request['idnumber'] ?? ''));
            break;
        case 'set_visible':
            $result = \local_truenorth\course_sync::set_visible((string) ($request['idnumber'] ?? ''),
                !empty($request['visible']));
            break;
        case 'delete_stage':
            $result = \local_truenorth\course_sync::delete_stage((string) ($request['idnumber'] ?? ''));
            break;
        case 'pull_results':
            // The answer is signed with this site's LTI key and names this ticket.
            $result = \local_truenorth\results::pull((string) ($request['cursor'] ?? ''),
                (int) ($request['limit'] ?? 500), (int) ($request['settle'] ?? 5), (string) $claims->jti);
            break;
        default:
            local_truenorth_reply(400, ['ok' => false, 'error' => 'unknown op']);
    }
} catch (\moodle_exception $e) {
    local_truenorth_reply(422, ['ok' => false, 'error' => $e->errorcode, 'detail' => $e->getMessage()]);
} catch (\Throwable $e) {
    // The detail (SQL, paths) stays in Moodle's log; TrueNorth gets the fact of failure.
    debugging($e->getMessage() . "\n" . $e->getTraceAsString(), DEBUG_DEVELOPER);
    error_log('local_truenorth: ' . $e->getMessage());
    local_truenorth_reply(500, ['ok' => false, 'error' => 'internal']);
}
local_truenorth_reply(200, ['ok' => true] + $result);
