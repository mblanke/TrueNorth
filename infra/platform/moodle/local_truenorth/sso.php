<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

/**
 * Sign-in hand-off from the TrueNorth app. Accepts only a POSTed ticket.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

require(__DIR__ . '/../../config.php');
require_once($CFG->libdir . '/enrollib.php');

$PAGE->set_context(context_system::instance());
$PAGE->set_url(new moodle_url('/local/truenorth/sso.php'));

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    // A ticket in a URL ends up in logs and history; refuse rather than accept it.
    throw new moodle_exception('ssodenied', 'local_truenorth');
}
$token = required_param('token', PARAM_RAW_TRIMMED);

$claims = \local_truenorth\sso::verify($token);
$user = \local_truenorth\sso::user_for($claims);

// Someone else signed in on this browser (a shared PC): drop everything their session
// held. Not require_logout(): it closes the session, so the login below would not stick.
// complete_user_login() regenerates the session id and destroys the old session itself.
if (isloggedin() && !isguestuser() && $USER->id != $user->id) {
    \core\event\user_loggedout::create(['userid' => $USER->id, 'objectid' => $USER->id,
        'other' => ['sessionid' => session_id()]])->trigger();
    \core\session\manager::init_empty_session();
}
$course = !empty($claims->course) ? \local_truenorth\sso::ensure_enrolled($user, $claims) : null;
complete_user_login($user);

redirect(\local_truenorth\sso::destination($course));
