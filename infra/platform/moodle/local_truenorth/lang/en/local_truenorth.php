<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * Strings for local_truenorth.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

defined('MOODLE_INTERNAL') || die();

$string['pluginname'] = 'TrueNorth integration';
$string['privacy:metadata'] = 'Stores only one-time ticket identifiers, which hold no personal data.';
$string['privacy:metadata:truenorth'] = 'When TrueNorth asks, the activity completions and quiz grades of accounts TrueNorth created, in TrueNorth courses, are sent to TrueNorth, which records them on the person\'s learning record.';
$string['privacy:metadata:truenorth:idnumber'] = 'The TrueNorth user id of the account (its ID number).';
$string['privacy:metadata:truenorth:completionstate'] = 'Whether an activity is complete, passed or failed.';
$string['privacy:metadata:truenorth:grade'] = 'The quiz grade, its maximum and pass mark, and the number of attempts.';
$string['privacy:metadata:truenorth:timemodified'] = 'When the completion or grade changed.';
$string['resultsnokey'] = 'This Moodle has no LTI site key to sign results with.';
$string['ssocoursemissing'] = 'This course is not available in Moodle yet. Ask your instructor.';
$string['ssodenied'] = 'TrueNorth sign-in was refused. Open the course again from the TrueNorth app.';
$string['ssoemailtaken'] = 'A different Moodle account already uses this email address. Ask an administrator to link it.';
$string['syncbadpayload'] = 'TrueNorth sent a course this Moodle could not read.';
$string['syncnotstage'] = 'Only a TrueNorth staging course can be deleted.';
$string['syncnoltitool'] = 'The TrueNorth Range LTI tool is not registered on this Moodle.';
$string['ssonotconfigured'] = 'TrueNorth sign-in is not configured on this Moodle.';
