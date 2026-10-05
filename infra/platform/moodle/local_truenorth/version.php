<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * TrueNorth integration: single sign-on and course sync from TrueNorth.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

defined('MOODLE_INTERNAL') || die();

$plugin->component = 'local_truenorth';
$plugin->version   = 2026100503;
$plugin->requires  = 2025041400; // Moodle 5.0.
$plugin->maturity  = MATURITY_ALPHA;
$plugin->release   = '0.3.1';
