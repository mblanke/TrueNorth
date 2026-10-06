<?php
// Plays one student through a published TrueNorth course inside a Moodle container:
// enrol, view the first page, attempt the first quiz, answer, resume, submit; then report
// the grade and completion as JSON. Used by tests/integration/test_moodle_publish.py.
//
//   php moodle_student.php --course=<idnumber> --username=<u> [--answers=right|wrong]
//                          [--leave-open]   (start an attempt, answer, do not submit)

define('CLI_SCRIPT', true);
require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/lib/enrollib.php');
require_once($CFG->dirroot . '/mod/quiz/locallib.php');
require_once($CFG->dirroot . '/lib/completionlib.php');
require_once($CFG->dirroot . '/mod/page/lib.php');
require_once($CFG->libdir . '/gradelib.php');

[$opt] = cli_get_params(['course' => '', 'username' => '', 'answers' => 'right', 'leave-open' => false]);
$course = $DB->get_record('course', ['idnumber' => $opt['course']], '*', MUST_EXIST);

// Student account and manual enrolment.
$user = $DB->get_record('user', ['username' => $opt['username']]);
if (!$user) {
    $id = user_create_user((object) ['username' => $opt['username'], 'password' => 'Student-Pass1!',
        'firstname' => 'Test', 'lastname' => 'Student', 'email' => $opt['username'] . '@example.test',
        'auth' => 'manual', 'confirmed' => 1, 'mnethostid' => $CFG->mnet_localhost_id], false, false);
    $user = $DB->get_record('user', ['id' => $id], '*', MUST_EXIST);
}
$manual = enrol_get_plugin('manual');
$instance = $DB->get_record('enrol', ['courseid' => $course->id, 'enrol' => 'manual'], '*', MUST_EXIST);
$role = $DB->get_record('role', ['shortname' => 'student'], '*', MUST_EXIST);
$manual->enrol_user($instance, $user->id, $role->id);
\core\session\manager::set_user($user);

$modinfo = get_fast_modinfo($course, $user->id);
$visible = [];
foreach ($modinfo->get_cms() as $cm) {
    if ($cm->uservisible && str_starts_with((string) $cm->idnumber, 'tn:')) {
        $visible[] = $cm;
    }
}
$page = null;
$quizcm = null;
foreach ($visible as $cm) {
    $page = $page ?? ($cm->modname === 'page' ? $cm : null);
    $quizcm = $quizcm ?? ($cm->modname === 'quiz' ? $cm : null);
}
if (!$page || !$quizcm) {
    cli_error('the student cannot see a page and a quiz');
}

// Open the page: completion on view.
$pagerec = $DB->get_record('page', ['id' => $page->instance], '*', MUST_EXIST);
page_view($pagerec, $course, $page, context_module::instance($page->id));

// The quiz: start (or resume) an attempt, answer every question, submit.
$quizobj = \mod_quiz\quiz_settings::create($quizcm->instance, $user->id);
$quba = null;
$attempt = quiz_get_user_attempt_unfinished($quizobj->get_quizid(), $user->id);
$resumed = (bool) $attempt;
if (!$attempt) {
    $quba = question_engine::make_questions_usage_by_activity('mod_quiz', $quizobj->get_context());
    $quba->set_preferred_behaviour($quizobj->get_quiz()->preferredbehaviour);
    $timenow = time();
    $attempt = quiz_create_attempt($quizobj, 1, null, $timenow, false, $user->id);
    quiz_start_new_attempt($quizobj, $quba, $attempt, 1, $timenow);
    quiz_attempt_save_started($quizobj, $quba, $attempt);
}
$attemptobj = \mod_quiz\quiz_attempt::create($attempt->id);
$answers = [];
foreach ($attemptobj->get_slots() as $slot) {
    $qa = $attemptobj->get_question_attempt($slot);
    $question = $qa->get_question();
    // Simulated responses name a multiple-choice answer by its text (see
    // qtype_multichoice_single_question::prepare_simulated_post_data).
    $choice = null;
    foreach ($question->answers as $answer) {
        if (($opt['answers'] === 'right') === ((float) $answer->fraction > 0)) {
            $choice = clean_param($answer->answer, PARAM_NOTAGS);
            break;
        }
    }
    $answers[$slot] = ['answer' => $choice];
}
$attemptobj->process_submitted_actions(time(), false, $answers);
$state = 'inprogress';
if (!$opt['leave-open']) {
    $attemptobj = \mod_quiz\quiz_attempt::create($attempt->id);
    $attemptobj->process_finish(time(), false);
    $state = 'finished';
}

$grade = grade_get_grades($course->id, 'mod', 'quiz', $quizcm->instance, $user->id);
$item = $grade->items[0] ?? null;
$completion = new completion_info($course);
echo json_encode([
    'courseid' => (int) $course->id,
    'visible_activities' => array_map(fn($cm) => $cm->idnumber, $visible),
    'quiz' => $quizcm->idnumber,
    'resumed' => $resumed,
    'attempt_state' => $state,
    'quiz_grade' => $item ? (float) ($item->grades[$user->id]->grade ?? -1) : null,
    'quiz_gradepass' => $item ? (float) $item->gradepass : null,
    'page_complete' => (int) $completion->get_data($page, false, $user->id)->completionstate === COMPLETION_COMPLETE,
    // Completion on a pass grade: COMPLETION_COMPLETE_PASS, not COMPLETION_COMPLETE_FAIL.
    'quiz_complete' => (int) $completion->get_data($quizcm, false, $user->id)->completionstate === COMPLETION_COMPLETE_PASS,
]), PHP_EOL;
