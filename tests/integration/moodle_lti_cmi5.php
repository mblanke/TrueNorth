<?php
// Moodle side of tests/integration/test_moodle_cmi5_lti.py, run inside the Moodle container.
//
//   php moodle_lti_cmi5.php --setup --resource=cmi5:<release>:<n> --username=<u>
//       A course with one "TrueNorth Range" External tool activity carrying the custom
//       parameter resource=<resource> (what a deep-linked cmi5 content item sets) and a
//       grade out of 100, and a Student enrolled in it. Prints JSON: course, cmid,
//       instance, userid, username, password, email.
//   php moodle_lti_cmi5.php --grade --course=<id> --instance=<lti id> --userid=<id>
//       The Student's grade for that activity in Moodle's gradebook, as JSON.

define('CLI_SCRIPT', true);
require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/course/lib.php');
require_once($CFG->dirroot . '/course/modlib.php');
require_once($CFG->dirroot . '/lib/enrollib.php');
require_once($CFG->dirroot . '/mod/lti/locallib.php');
require_once($CFG->libdir . '/gradelib.php');

[$opt] = cli_get_params(['setup' => false, 'grade' => false, 'resource' => '', 'username' => '', 'course' => 0,
    'instance' => 0, 'userid' => 0]);

if ($opt['grade']) {
    $grades = grade_get_grades((int) $opt['course'], 'mod', 'lti', (int) $opt['instance'], [(int) $opt['userid']]);
    $item = $grades->items[0] ?? null;
    $grade = $item ? ($item->grades[(int) $opt['userid']] ?? null) : null;
    echo json_encode([
        'grademax' => $item ? (float) $item->grademax : null,
        'grade' => ($grade && $grade->grade !== null) ? (float) $grade->grade : null,
    ]), PHP_EOL;
    exit(0);
}

if (!$opt['setup'] || !$opt['resource'] || !$opt['username']) {
    cli_error('usage: --setup --resource=<r> --username=<u> | --grade --course= --instance= --userid=');
}
\core\session\manager::set_user(get_admin());
$typeid = $DB->get_field('lti_types', 'id', ['name' => 'TrueNorth Range', 'course' => SITEID], MUST_EXIST);
$suffix = substr(md5($opt['username']), 0, 8);
$course = create_course((object) ['fullname' => "cmi5 over LTI $suffix", 'shortname' => "cmi5-lti-$suffix",
    'category' => 1, 'enablecompletion' => 1]);

$password = 'Student-Pass1!';
$email = $opt['username'] . '@example.test';
$user = $DB->get_record('user', ['username' => $opt['username']]);
if (!$user) {
    $id = user_create_user((object) ['username' => $opt['username'], 'password' => $password,
        'firstname' => 'Test', 'lastname' => 'Student', 'email' => $email,
        'auth' => 'manual', 'confirmed' => 1, 'mnethostid' => $CFG->mnet_localhost_id], true, false);
    $user = $DB->get_record('user', ['id' => $id], '*', MUST_EXIST);
}
$manual = enrol_get_plugin('manual');
$instance = $DB->get_record('enrol', ['courseid' => $course->id, 'enrol' => 'manual'], '*', MUST_EXIST);
$role = $DB->get_record('role', ['shortname' => 'student'], '*', MUST_EXIST);
$manual->enrol_user($instance, $user->id, $role->id);

$module = $DB->get_record('modules', ['name' => 'lti'], '*', MUST_EXIST);
$info = add_moduleinfo((object) [
    'modulename' => 'lti', 'module' => $module->id, 'course' => $course->id, 'section' => 0, 'visible' => 1,
    'name' => 'Module 1 (cmi5)', 'introeditor' => ['text' => '', 'format' => FORMAT_HTML, 'itemid' => 0],
    'typeid' => (int) $typeid, 'toolurl' => '',
    'instructorcustomparameters' => 'resource=' . $opt['resource'],
    'launchcontainer' => LTI_LAUNCH_CONTAINER_WINDOW,
    'instructorchoicesendname' => 1, 'instructorchoicesendemailaddr' => 1, 'instructorchoiceacceptgrades' => 1,
    'grade' => 100, 'cmidnumber' => '',
], $course);

echo json_encode([
    'course' => (int) $course->id, 'cmid' => (int) $info->coursemodule, 'instance' => (int) $info->instance,
    'userid' => (int) $user->id, 'username' => $opt['username'], 'password' => $password, 'email' => $email,
]), PHP_EOL;
