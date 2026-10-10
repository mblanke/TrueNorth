<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

namespace local_truenorth\privacy;

use core_privacy\local\metadata\collection;
use core_privacy\local\request\approved_contextlist;
use core_privacy\local\request\approved_userlist;
use core_privacy\local\request\contextlist;
use core_privacy\local\request\userlist;

/**
 * The plugin stores no personal data (its ticket ids are random and unlinked to users),
 * but it sends some to TrueNorth: the activity completions and quiz grades of the
 * accounts TrueNorth sign-in created, identified by their TrueNorth user id
 * (classes/results.php). That is declared as an external location. There is nothing
 * here to export or delete; those records live in TrueNorth.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class provider implements
    \core_privacy\local\metadata\provider,
    \core_privacy\local\request\core_userlist_provider,
    \core_privacy\local\request\plugin\provider {

    public static function get_metadata(collection $collection): collection {
        $collection->add_external_location_link('truenorth', [
            'idnumber' => 'privacy:metadata:truenorth:idnumber',
            'completionstate' => 'privacy:metadata:truenorth:completionstate',
            'grade' => 'privacy:metadata:truenorth:grade',
            'timemodified' => 'privacy:metadata:truenorth:timemodified',
        ], 'privacy:metadata:truenorth');
        return $collection;
    }

    public static function get_contexts_for_userid(int $userid): contextlist {
        return new contextlist();
    }

    public static function get_users_in_context(userlist $userlist) {
    }

    public static function export_user_data(approved_contextlist $contextlist) {
    }

    public static function delete_data_for_all_users_in_context(\context $context) {
    }

    public static function delete_data_for_user(approved_contextlist $contextlist) {
    }

    public static function delete_data_for_users(approved_userlist $userlist) {
    }
}
