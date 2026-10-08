<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

namespace local_truenorth;

use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use moodle_exception;
use stdClass;

/**
 * Verifies the short-lived tickets TrueNorth signs with its LTI tool key (RS256).
 *
 * TrueNorth is this Moodle's only authority: sign-in hand-offs (`typ` = "sso") and
 * course sync calls (`typ` = "sync") are both tickets. The `typ` keeps one kind from
 * being replayed as the other. Each ticket is single use: its `jti` is burnt here.
 *
 * Configuration (set by the farm bootstrap, `truenorth_setup.php --sso-publickey=…`):
 *   local_truenorth/ssopublickey  TrueNorth's public key, PEM.
 *   local_truenorth/ssoissuer     expected `iss`, default "truenorth".
 *   local_truenorth/tenantid      the TrueNorth tenant this node serves (`--tenant-id`);
 *                                 every ticket's `tid` must equal it, and with it unset
 *                                 no ticket is accepted.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class ticket {
    /** Longest ticket lifetime accepted, whatever the ticket itself claims. */
    const MAX_LIFETIME = 120;

    /** Clock skew tolerated between TrueNorth and Moodle. */
    const LEEWAY = 30;

    /**
     * Verify a ticket of the given type and burn its jti.
     *
     * @param string $token compact JWT
     * @param string $typ expected `typ` claim ("sso" or "sync")
     * @return stdClass the ticket's claims
     * @throws moodle_exception on anything but a fresh, valid, unused ticket
     */
    public static function verify(string $token, string $typ): stdClass {
        global $CFG, $DB;

        $pem = trim((string) get_config('local_truenorth', 'ssopublickey'));
        if ($pem === '') {
            throw new moodle_exception('ssonotconfigured', 'local_truenorth');
        }
        JWT::$leeway = self::LEEWAY;
        try {
            $claims = JWT::decode($token, new Key($pem, 'RS256'));
        } catch (\Throwable $e) {
            throw new moodle_exception('ssodenied', 'local_truenorth', '', null, $e->getMessage());
        }

        $issuer = get_config('local_truenorth', 'ssoissuer') ?: 'truenorth';
        $aud = is_array($claims->aud ?? null) ? $claims->aud : [$claims->aud ?? ''];
        $ok = ($claims->iss ?? '') === $issuer
            && ($claims->typ ?? '') === $typ
            && in_array(rtrim($CFG->wwwroot, '/'), $aud, true)
            && !empty($claims->jti) && strlen($claims->jti) <= 64
            && !empty($claims->iat) && !empty($claims->exp)
            && ($claims->exp - $claims->iat) <= self::MAX_LIFETIME;
        if ($ok) {
            // One TrueNorth key signs for every tenant: every ticket, sign-in or sync, must
            // name the tenant this Moodle serves, or one tenant's ticket could be replayed
            // into another's site. The audience alone is not enough: it is the platform's
            // lti_issuer, which a tenant's integration admin can set.
            $tenant = (string) get_config('local_truenorth', 'tenantid');
            $ok = $tenant !== '' && hash_equals($tenant, (string) ($claims->tid ?? ''));
        }
        if (!$ok) {
            throw new moodle_exception('ssodenied', 'local_truenorth', '', null, 'claims');
        }

        // Single use: the unique index makes a second insert of the same jti fail.
        $DB->delete_records_select('local_truenorth_sso_jti', 'expires < ?', [time()]);
        try {
            $DB->insert_record('local_truenorth_sso_jti', (object) [
                'jti' => $claims->jti,
                'expires' => (int) $claims->exp + self::LEEWAY,
            ]);
        } catch (\dml_exception $e) {
            throw new moodle_exception('ssodenied', 'local_truenorth', '', null, 'replay');
        }
        return $claims;
    }
}
