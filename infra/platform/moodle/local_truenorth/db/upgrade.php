<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * Upgrade steps for local_truenorth.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

/**
 * @param int $oldversion
 * @return bool
 */
function xmldb_local_truenorth_upgrade($oldversion) {
    if ($oldversion < 2026100901) {
        // Results are reported under the idnumber sign-in sets: Students must not edit it.
        \local_truenorth\sso::lock_profile_fields();
        upgrade_plugin_savepoint(true, 2026100901, 'local', 'truenorth');
    }
    return true;
}
