<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

namespace local_truenorth;

use moodle_exception;
use moodle_url;
use stdClass;

/**
 * Single sign-on hand-off from the TrueNorth app.
 *
 * TrueNorth decides who may open a course and signs a one-minute, single-use ticket
 * (see {@see ticket}). This class resolves the TrueNorth user to a Moodle account
 * through `idnumber` (= TrueNorth user UUID), creating the account and the course
 * enrolment if they do not exist yet.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class sso {
    /**
     * Verify a sign-in ticket (`typ` = "sso") and burn it.
     *
     * @param string $token compact JWT posted by the browser
     * @return stdClass the ticket's claims
     */
    public static function verify(string $token): stdClass {
        $claims = ticket::verify($token, 'sso');
        if (empty($claims->sub) || !in_array($claims->role ?? '', ['student', 'teacher'], true)) {
            throw new moodle_exception('ssodenied', 'local_truenorth', '', null, 'claims');
        }
        return $claims;
    }

    /**
     * The Moodle account for a TrueNorth user, created on first use.
     *
     * Matching is by idnumber only. An existing account that merely shares the email
     * is never taken over: that is how a platform could otherwise become an admin.
     *
     * @param stdClass $claims verified ticket claims
     * @return stdClass full user record
     */
    public static function user_for(stdClass $claims): stdClass {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/user/lib.php');

        $matches = $DB->get_records('user', [
            'idnumber' => $claims->sub,
            'deleted' => 0,
            'mnethostid' => $CFG->mnet_localhost_id,
        ]);
        if (count($matches) > 1) {
            throw new moodle_exception('ssodenied', 'local_truenorth', '', null, 'duplicate idnumber');
        }
        $user = $matches ? reset($matches) : null;

        $email = clean_param($claims->email ?? '', PARAM_EMAIL);
        $profile = [
            'firstname' => clean_param($claims->given_name ?? '', PARAM_NOTAGS) ?: '-',
            'lastname' => clean_param($claims->family_name ?? '', PARAM_NOTAGS) ?: '-',
            'email' => $email,
        ];

        if ($user) {
            if ($user->suspended) {
                throw new moodle_exception('ssodenied', 'local_truenorth', '', null, 'suspended');
            }
            $changed = array_filter($profile, fn($v, $k) => $v !== '' && $user->$k !== $v, ARRAY_FILTER_USE_BOTH);
            if ($changed) {
                user_update_user((object) (['id' => $user->id] + $changed), false, false);
            }
            return get_complete_user_data('id', $user->id);
        }

        if ($email !== '' && $DB->record_exists_select(
            'user', 'LOWER(email) = LOWER(?) AND deleted = 0', [$email])) {
            throw new moodle_exception('ssoemailtaken', 'local_truenorth');
        }
        $new = (object) ($profile + [
            'username' => 'tn-' . strtolower(clean_param($claims->sub, PARAM_ALPHANUMEXT)),
            'idnumber' => $claims->sub,
            'auth' => 'manual',
            // Nobody knows this password, so the account can only be entered through TrueNorth.
            'password' => random_string(48) . '!Aa1',
            'confirmed' => 1,
            'mnethostid' => $CFG->mnet_localhost_id,
        ]);
        $id = user_create_user($new, true, true);
        return get_complete_user_data('id', $id);
    }

    /**
     * Make sure the user is enrolled in the course TrueNorth named, with the right role.
     *
     * @param stdClass $user Moodle user
     * @param stdClass $claims verified ticket claims (must include `course`)
     * @return stdClass the Moodle course
     */
    public static function ensure_enrolled(stdClass $user, stdClass $claims): stdClass {
        global $DB;

        $course = $DB->get_record('course', ['idnumber' => $claims->course]);
        if (!$course) {
            throw new moodle_exception('ssocoursemissing', 'local_truenorth');
        }
        $context = \context_course::instance($course->id);
        $archetype = $claims->role === 'teacher' ? 'editingteacher' : 'student';
        $roles = get_archetype_roles($archetype);
        $role = reset($roles);

        $manual = enrol_get_plugin('manual');
        $instance = null;
        foreach (enrol_get_instances($course->id, false) as $candidate) {
            if ($candidate->enrol === 'manual') {
                $instance = $candidate;
                break;
            }
        }
        if (!$instance) {
            $instance = $DB->get_record('enrol', ['id' => $manual->add_default_instance($course)], '*', MUST_EXIST);
        }
        if ($instance->status != ENROL_INSTANCE_ENABLED) {
            $manual->update_status($instance, ENROL_INSTANCE_ENABLED);
        }

        $enrolment = $DB->get_record('user_enrolments', ['enrolid' => $instance->id, 'userid' => $user->id]);
        if (!$enrolment) {
            $manual->enrol_user($instance, $user->id, $role->id);
        } else {
            if ($enrolment->status != ENROL_USER_ACTIVE) {
                $manual->update_user_enrol($instance, $user->id, ENROL_USER_ACTIVE);
            }
            if (!user_has_role_assignment($user->id, $role->id, $context->id)) {
                role_assign($role->id, $user->id, $context->id);
            }
        }
        return $course;
    }

    /**
     * Where to send the user after sign-in.
     *
     * @param stdClass|null $course the course named by the ticket, if any
     * @return moodle_url
     */
    public static function destination(?stdClass $course): moodle_url {
        return $course ? new moodle_url('/course/view.php', ['id' => $course->id]) : new moodle_url('/my/');
    }
}
