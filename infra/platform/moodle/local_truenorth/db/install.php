<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * Install steps for local_truenorth.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

/**
 * Lock the idnumber of TrueNorth-created accounts (results are reported under it).
 *
 * @return bool
 */
function xmldb_local_truenorth_install() {
    \local_truenorth\sso::lock_profile_fields();
    return true;
}
