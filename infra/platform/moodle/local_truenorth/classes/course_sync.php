<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

namespace local_truenorth;

use moodle_exception;
use stdClass;

/**
 * Makes a Moodle course match the TrueNorth course it projects.
 *
 * TrueNorth is the source of truth. It sends the whole course on every change and this
 * class converges Moodle on it: the category, the course, one section per TrueNorth
 * module, and the activities TrueNorth owns. Everything is keyed by idnumber:
 *   category  `tn-qual:<qualification uuid>` (or `tn-catalogue`)
 *   course    `<course uuid>`, or `tn-stage:<release uuid>` for a release being staged
 *   activity  `tn:<module>:<slot>`  (slot = page… | quiz:<question hash> | lab)
 * Activities without a `tn:` idnumber were added in Moodle by an instructor; they are
 * never touched or deleted here.
 *
 * A TrueNorth activity that is no longer wanted is deleted only when no student has
 * used it; one with attempts or grades is hidden instead, so a new release never takes
 * away a student's work. A quiz is named by a hash of its questions: changed questions
 * make a new quiz rather than rewriting one students have attempted.
 *
 * Core web services cannot name sections or create activities (MDL-37083), which is
 * why this lives in a plugin and calls Moodle's internal APIs.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class course_sync {
    /** Activity types TrueNorth projects. */
    const TYPES = ['page', 'lti', 'quiz'];

    /** Staging courses: the only courses delete() will remove. */
    const STAGE_PREFIX = 'tn-stage:';

    /**
     * Create or update a course from a TrueNorth payload.
     *
     * @param array $p payload: idnumber, fullname, shortname, summary, visible,
     *                 category{idnumber,name}, sections[{name, summary, activities[…]}]
     * @return array courseid, created, activities {idnumber: cmid}, removed [idnumber]
     */
    public static function upsert(array $p): array {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        require_once($CFG->dirroot . '/course/modlib.php');

        self::check_payload($p);
        $category = self::category($p['category']);
        $sections = array_values($p['sections']);

        $course = $DB->get_record('course', ['idnumber' => $p['idnumber']]);
        $created = !$course;
        $fields = [
            'fullname' => $p['fullname'],
            'summary' => $p['summary'] ?? '',
            'summaryformat' => FORMAT_MARKDOWN,
            'category' => $category->id,
            'visible' => empty($p['visible']) ? 0 : 1,
        ];
        if ($created) {
            $course = create_course((object) ($fields + [
                'idnumber' => $p['idnumber'],
                'shortname' => self::free_shortname($p['shortname'], $p['idnumber']),
                'format' => 'topics',
                'numsections' => count($sections),
                'enablecompletion' => 1,
            ]));
        } else {
            update_course((object) (['id' => $course->id] + $fields));
        }
        $course = get_course($course->id);

        course_create_sections_if_missing($course, range(0, count($sections)));
        $wanted = [];
        foreach ($sections as $i => $s) {
            $num = $i + 1;
            $section = $DB->get_record('course_sections', ['course' => $course->id, 'section' => $num], '*', MUST_EXIST);
            course_update_section($course, $section, [
                'name' => $s['name'],
                'summary' => $s['summary'] ?? '',
                'summaryformat' => FORMAT_MARKDOWN,
                'visible' => 1,
            ]);
            foreach ($s['activities'] ?? [] as $a) {
                $wanted[$a['idnumber']] = $a + ['section' => $num];
            }
        }

        $existing = self::owned_activities($course->id);
        $activities = [];
        foreach ($wanted as $idnumber => $a) {
            $cm = $existing[$idnumber] ?? null;
            $activities[$idnumber] = $cm ? self::update_activity($course, $cm, $a) : self::add_activity($course, $a);
        }

        $removed = [];
        $retired = [];
        foreach (array_diff_key($existing, $wanted) as $idnumber => $cm) {
            if (self::has_user_data($cm)) {
                set_coursemodule_visible($cm->id, 0);
                $retired[] = $idnumber;
            } else {
                course_delete_module($cm->id);
                $removed[] = $idnumber;
            }
        }
        self::drop_empty_trailing_sections($course, count($sections));
        rebuild_course_cache($course->id, true);

        return [
            'courseid' => (int) $course->id,
            'created' => $created,
            'activities' => $activities,
            'removed' => $removed,
            'retired' => $retired,
        ];
    }

    /**
     * What this Moodle holds for a TrueNorth course: enough for TrueNorth to verify a
     * staged release and to reconcile an interrupted publication.
     *
     * @param string $idnumber course idnumber
     * @return array exists, courseid, visible, sections, activities {idnumber: {cmid, type, visible, questions}}
     */
    public static function describe(string $idnumber): array {
        if (!self::is_tn_course($idnumber)) {
            throw new moodle_exception('syncbadpayload', 'local_truenorth');
        }
        global $DB;
        $course = $idnumber === '' ? false : $DB->get_record('course', ['idnumber' => $idnumber]);
        if (!$course) {
            return ['exists' => false];
        }
        $activities = [];
        foreach (self::owned_activities($course->id) as $id => $cm) {
            $row = ['cmid' => (int) $cm->id, 'type' => $cm->modname, 'visible' => (int) $cm->visible,
                'section' => (int) $cm->sectionnum];
            if ($cm->modname === 'quiz') {
                $row['questions'] = $DB->count_records('quiz_slots', ['quizid' => $cm->instance]);
            }
            if ($cm->modname === 'page') {
                $row['content_length'] = strlen((string) $DB->get_field('page', 'content', ['id' => $cm->instance]));
            }
            $activities[$id] = $row;
        }
        return [
            'exists' => true,
            'courseid' => (int) $course->id,
            'visible' => (int) $course->visible,
            'sections' => $DB->count_records_select('course_sections', 'course = ? AND section > 0', [$course->id]),
            'activities' => $activities,
            'ltitool' => (bool) $DB->get_field('lti_types', 'id', ['name' => 'TrueNorth Range', 'course' => SITEID]),
        ];
    }

    /**
     * Show or hide a course.
     *
     * @param string $idnumber
     * @param bool $visible
     * @return array courseid (0 when absent)
     */
    public static function set_visible(string $idnumber, bool $visible): array {
        if (!self::is_tn_course($idnumber)) {
            throw new moodle_exception('syncbadpayload', 'local_truenorth');
        }
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        $id = $idnumber === '' ? false : $DB->get_field('course', 'id', ['idnumber' => $idnumber]);
        if ($id) {
            update_course((object) ['id' => $id, 'visible' => $visible ? 1 : 0]);
        }
        return ['courseid' => (int) $id];
    }

    /**
     * Delete a staging course after its release went live. Refuses any other course:
     * a live course holds students' history and is only ever hidden.
     *
     * @param string $idnumber must start with STAGE_PREFIX
     * @return array deleted
     */
    public static function delete_stage(string $idnumber): array {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        if (!str_starts_with($idnumber, self::STAGE_PREFIX)) {
            throw new moodle_exception('syncnotstage', 'local_truenorth');
        }
        $course = $DB->get_record('course', ['idnumber' => $idnumber]);
        if (!$course) {
            return ['deleted' => false];
        }
        delete_course($course, false);
        return ['deleted' => true];
    }

    /**
     * Whether students have used this activity: a quiz attempt or any grade on it.
     *
     * @param stdClass $cm row from owned_activities()
     * @return bool
     */
    private static function has_user_data(stdClass $cm): bool {
        global $DB;
        if ($cm->modname === 'quiz' && $DB->record_exists('quiz_attempts', ['quiz' => $cm->instance, 'preview' => 0])) {
            return true;
        }
        return $DB->record_exists_sql(
            "SELECT 1 FROM {grade_grades} gg
               JOIN {grade_items} gi ON gi.id = gg.itemid
              WHERE gi.itemtype = 'mod' AND gi.itemmodule = ? AND gi.iteminstance = ? AND gg.finalgrade IS NOT NULL",
            [$cm->modname, $cm->instance]
        );
    }

    /**
     * Hide a course TrueNorth no longer offers. Nothing is deleted: grades and history stay.
     *
     * @param string $idnumber TrueNorth course uuid
     * @return array courseid (0 when Moodle never had it)
     */
    public static function hide(string $idnumber): array {
        if (!self::is_tn_course($idnumber)) {
            throw new moodle_exception('syncbadpayload', 'local_truenorth');
        }
        global $CFG, $DB;
        require_once($CFG->dirroot . '/course/lib.php');
        $id = $idnumber === '' ? false : $DB->get_field('course', 'id', ['idnumber' => $idnumber]);
        if ($id) {
            update_course((object) ['id' => $id, 'visible' => 0]);
        }
        return ['courseid' => (int) $id];
    }

    /**
     * Refuse a malformed payload before changing anything.
     *
     * @param array $p
     */
    private static function check_payload(array $p): void {
        $ok = self::is_tn_course((string) ($p['idnumber'] ?? '')) && !empty($p['fullname']) && !empty($p['shortname'])
            && str_starts_with((string) ($p['category']['idnumber'] ?? ''), 'tn-') && !empty($p['category']['name'])
            && isset($p['sections']) && is_array($p['sections']);
        foreach ($p['sections'] ?? [] as $s) {
            $ok = $ok && !empty($s['name']);
            foreach ($s['activities'] ?? [] as $a) {
                $ok = $ok && str_starts_with($a['idnumber'] ?? '', 'tn:') && !empty($a['name'])
                    && in_array($a['type'] ?? '', self::TYPES, true)
                    && ($a['type'] !== 'lti' || !empty($a['resource']))
                    && ($a['type'] !== 'quiz' || self::questions_ok($a['questions'] ?? null));
            }
        }
        if (!$ok) {
            throw new moodle_exception('syncbadpayload', 'local_truenorth');
        }
    }

    /**
     * Only courses TrueNorth created: idnumber is a TrueNorth course UUID or a staging
     * `tn-stage:<release UUID>`. Nothing here can reach a course someone made in Moodle.
     *
     * @param string $idnumber
     * @return bool
     */
    private static function is_tn_course(string $idnumber): bool {
        return (bool) preg_match('/^(tn-stage:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/', $idnumber);
    }

    /**
     * Authored HTML is cleaned with Moodle's purifier before it is stored: TrueNorth content
     * must never run script for the students and admins who view it.
     *
     * @param string $text
     * @param string|int $format a FORMAT_* constant (Moodle defines them as strings)
     * @return string
     */
    private static function clean(string $text, $format): string {
        return (string) $format === (string) FORMAT_HTML ? clean_text($text, FORMAT_HTML) : $text;
    }

    /**
     * Multiple-choice questions: text, 2+ answers, at least one correct index in range.
     *
     * @param mixed $questions
     * @return bool
     */
    private static function questions_ok($questions): bool {
        if (!is_array($questions) || !$questions) {
            return false;
        }
        foreach ($questions as $q) {
            $answers = $q['answers'] ?? null;
            $correct = $q['correct'] ?? null;
            if (empty($q['text']) || !is_array($answers) || count($answers) < 2 || !is_array($correct) || !$correct) {
                return false;
            }
            foreach ($correct as $i) {
                if (!is_int($i) || $i < 0 || $i >= count($answers)) {
                    return false;
                }
            }
        }
        return true;
    }

    /**
     * The category for this course, created or renamed to match.
     *
     * @param array $c idnumber, name
     * @return \core_course_category
     */
    private static function category(array $c): \core_course_category {
        global $DB;
        $id = $DB->get_field('course_categories', 'id', ['idnumber' => $c['idnumber']]);
        if (!$id) {
            return \core_course_category::create(['name' => $c['name'], 'idnumber' => $c['idnumber'], 'parent' => 0]);
        }
        $category = \core_course_category::get($id, MUST_EXIST, true);
        if ($category->name !== $c['name']) {
            $category->update(['name' => $c['name']]);
        }
        return $category;
    }

    /**
     * Shortnames are unique site-wide; disambiguate a clash with the TrueNorth id.
     *
     * @param string $shortname wanted shortname
     * @param string $idnumber TrueNorth course uuid
     * @return string
     */
    private static function free_shortname(string $shortname, string $idnumber): string {
        global $DB;
        if (!$DB->record_exists('course', ['shortname' => $shortname])) {
            return $shortname;
        }
        return $shortname . ' [' . substr($idnumber, 0, 8) . ']';
    }

    /**
     * TrueNorth-owned course modules in this course, by idnumber.
     *
     * @param int $courseid
     * @return stdClass[]
     */
    private static function owned_activities(int $courseid): array {
        global $DB;
        $rows = $DB->get_records_sql(
            "SELECT cm.id, cm.idnumber, cm.instance, cm.visible, cs.section AS sectionnum, m.name AS modname
               FROM {course_modules} cm
               JOIN {modules} m ON m.id = cm.module
               JOIN {course_sections} cs ON cs.id = cm.section
              WHERE cm.course = ? AND cm.deletioninprogress = 0 AND " . $DB->sql_like('cm.idnumber', '?'),
            [$courseid, 'tn:%']
        );
        $out = [];
        foreach ($rows as $row) {
            $out[$row->idnumber] = $row;
        }
        return $out;
    }

    /**
     * Fields shared by add and update, per activity type.
     *
     * @param array $a activity from the payload
     * @return array
     */
    private static function type_fields(array $a): array {
        global $DB;
        $intro = ['text' => $a['intro'] ?? '', 'format' => FORMAT_MARKDOWN, 'itemid' => 0];
        if ($a['type'] === 'page') {
            $format = ($a['format'] ?? 'markdown') === 'html' ? FORMAT_HTML : FORMAT_MARKDOWN;
            return [
                'name' => $a['name'],
                'introeditor' => $intro,
                'page' => ['text' => self::clean($a['content'] ?? '', $format), 'format' => $format, 'itemid' => 0],
                // page_add_instance/page_update_instance copy `page` into `content` only when a
                // form is passed; called from code there is none, so set the columns directly.
                'content' => self::clean($a['content'] ?? '', $format),
                'contentformat' => $format,
                'display' => 5, 'printintro' => 0, 'printlastmodified' => 0,
                'completion' => COMPLETION_TRACKING_AUTOMATIC, 'completionview' => 1,
            ];
        }
        if ($a['type'] === 'quiz') {
            return self::quiz_fields($a, $intro);
        }
        $typeid = $DB->get_field('lti_types', 'id', ['name' => 'TrueNorth Range', 'course' => SITEID]);
        if (!$typeid) {
            throw new moodle_exception('syncnoltitool', 'local_truenorth');
        }
        return [
            'name' => $a['name'],
            'introeditor' => $intro,
            'typeid' => (int) $typeid,
            'toolurl' => '',
            'instructorcustomparameters' => 'resource=' . $a['resource'],
            'launchcontainer' => LTI_LAUNCH_CONTAINER_WINDOW,
            'instructorchoicesendname' => 1,
            'instructorchoicesendemailaddr' => 1,
            'instructorchoiceacceptgrades' => 1,
            'grade' => (int) ($a['grade'] ?? 100),
            'completion' => COMPLETION_TRACKING_AUTOMATIC, 'completionview' => 0, 'completionusegrade' => 1,
        ];
    }

    /**
     * Create a TrueNorth-owned activity.
     *
     * @param stdClass $course
     * @param array $a activity, with its section number
     * @return int the new cmid
     */
    private static function add_activity(stdClass $course, array $a): int {
        global $CFG, $DB;
        $modname = $a['type'];
        if ($modname === 'lti') {
            require_once($CFG->dirroot . '/mod/lti/locallib.php');
        }
        if ($modname === 'quiz') {
            require_once($CFG->dirroot . '/mod/quiz/locallib.php');
        }
        $module = $DB->get_record('modules', ['name' => $modname], '*', MUST_EXIST);
        $data = (object) (self::type_fields($a) + [
            'modulename' => $modname,
            'module' => $module->id,
            'course' => $course->id,
            'section' => $a['section'],
            'visible' => 1,
            'cmidnumber' => $a['idnumber'],
        ]);
        $info = add_moduleinfo($data, $course);
        if ($modname === 'quiz') {
            self::add_questions((int) $info->instance, (int) $info->coursemodule, $a['questions']);
        }
        return (int) $info->coursemodule;
    }

    /**
     * Quiz settings: one attempt grade (highest), no time limit, feedback after
     * submission, pass mark from TrueNorth, completion on passing.
     *
     * @param array $a quiz activity
     * @param array $intro intro editor value
     * @return array
     */
    private static function quiz_fields(array $a, array $intro): array {
        $grade = (float) ($a['grade'] ?? 100);
        $review = [];
        foreach (['attempt', 'correctness', 'maxmarks', 'marks', 'specificfeedback', 'generalfeedback', 'rightanswer',
                     'overallfeedback'] as $what) {
            // The right answer only once the quiz is closed: shown straight after an
            // attempt, it turns the next attempt into copying.
            $right = $what !== 'rightanswer';
            $review[$what . 'immediately'] = (int) $right;
            $review[$what . 'open'] = (int) $right;
            $review[$what . 'closed'] = 1;
        }
        return $review + [
            'name' => $a['name'],
            'introeditor' => $intro,
            'timeopen' => 0, 'timeclose' => 0, 'timelimit' => 0,
            'overduehandling' => 'autosubmit', 'graceperiod' => 0,
            'preferredbehaviour' => 'deferredfeedback',
            'attempts' => (int) ($a['attempts'] ?? 0), 'attemptonlast' => 0,
            'grademethod' => QUIZ_GRADEHIGHEST, 'decimalpoints' => 2, 'questiondecimalpoints' => -1,
            'questionsperpage' => 1, 'navmethod' => QUIZ_NAVMETHOD_FREE, 'shuffleanswers' => 1,
            'sumgrades' => 0, 'grade' => $grade,
            'gradepass' => round($grade * (float) ($a['pass_pct'] ?? 70) / 100, 2),
            'quizpassword' => '', 'subnet' => '', 'browsersecurity' => '-',
            'delay1' => 0, 'delay2' => 0, 'showuserpicture' => 0, 'showblocks' => 0,
            'completion' => COMPLETION_TRACKING_AUTOMATIC, 'completionusegrade' => 1, 'completionpassgrade' => 1,
        ];
    }

    /**
     * Put the release's multiple-choice questions in the quiz's own question bank and
     * on the quiz, one per page, in order.
     *
     * @param int $quizid
     * @param int $cmid
     * @param array $questions [{name?, text, answers[], correct[]}]
     */
    private static function add_questions(int $quizid, int $cmid, array $questions): void {
        global $CFG, $DB;
        require_once($CFG->dirroot . '/question/engine/bank.php');
        require_once($CFG->libdir . '/questionlib.php');
        $context = \context_module::instance($cmid);
        $category = question_get_default_category($context->id, true);
        $quiz = $DB->get_record('quiz', ['id' => $quizid], '*', MUST_EXIST);
        $quiz->cmid = $cmid;
        $qtype = \question_bank::get_qtype('multichoice');
        foreach (array_values($questions) as $i => $q) {
            $answers = array_values($q['answers']);
            $correct = array_values($q['correct']);
            $share = 1.0 / count($correct);
            $form = (object) [
                'category' => $category->id . ',' . $context->id,
                'name' => $q['name'] ?? ('Q' . ($i + 1)),
                'questiontext' => ['text' => self::clean((string) $q['text'], FORMAT_HTML), 'format' => FORMAT_HTML],
                'generalfeedback' => ['text' => $q['rationale'] ?? '', 'format' => FORMAT_HTML],
                'defaultmark' => 1,
                'penalty' => 0.3333333,
                'single' => count($correct) === 1 ? 1 : 0,
                'shuffleanswers' => 1,
                'answernumbering' => 'abc',
                'showstandardinstruction' => 0,
                'shownumcorrect' => 1,
                'correctfeedback' => ['text' => '', 'format' => FORMAT_HTML],
                'partiallycorrectfeedback' => ['text' => '', 'format' => FORMAT_HTML],
                'incorrectfeedback' => ['text' => '', 'format' => FORMAT_HTML],
                'noanswers' => count($answers),
                'answer' => [], 'fraction' => [], 'feedback' => [],
                'status' => \core_question\local\bank\question_version_status::QUESTION_STATUS_READY,
            ];
            foreach ($answers as $j => $text) {
                $form->answer[$j] = ['text' => (string) $text, 'format' => FORMAT_PLAIN];
                $form->fraction[$j] = in_array($j, $correct, true) ? (string) $share : '0.0';
                $form->feedback[$j] = ['text' => '', 'format' => FORMAT_HTML];
            }
            $question = new stdClass();
            $question->category = $category->id;
            $question->qtype = 'multichoice';
            $question->createdby = get_admin()->id;
            $saved = $qtype->save_question($question, $form);
            quiz_add_quiz_question($saved->id, $quiz, $i + 1, 1);
        }
        \mod_quiz\quiz_settings::create($quizid)->get_grade_calculator()->recompute_quiz_sumgrades();
    }

    /**
     * Bring an existing TrueNorth-owned activity up to date, moving it if its module moved.
     *
     * @param stdClass $course
     * @param stdClass $owned row from owned_activities()
     * @param array $a activity, with its section number
     * @return int the cmid
     */
    private static function update_activity(stdClass $course, stdClass $owned, array $a): int {
        global $CFG, $DB;
        if ($owned->modname !== $a['type']) {
            // A slot changed type (say a lab became a reading): replace it.
            course_delete_module($owned->id);
            return self::add_activity($course, $a);
        }
        if ($a['type'] === 'lti') {
            require_once($CFG->dirroot . '/mod/lti/locallib.php');
        }
        if ($a['type'] === 'quiz') {
            // The idnumber carries the question hash: same idnumber, same questions. Only
            // the settings are brought up to date; the questions are never rewritten.
            require_once($CFG->dirroot . '/mod/quiz/locallib.php');
        }
        if (!(int) $owned->visible) {
            set_coursemodule_visible($owned->id, 1);
        }
        $cm = get_coursemodule_from_id($a['type'], $owned->id, $course->id, false, MUST_EXIST);
        [$cm, , , $data] = get_moduleinfo_data($cm, $course);
        foreach (self::type_fields($a) as $field => $value) {
            $data->$field = $value;
        }
        update_moduleinfo($cm, $data, $course);

        if ((int) $owned->sectionnum !== (int) $a['section']) {
            $section = $DB->get_record('course_sections', ['course' => $course->id, 'section' => $a['section']], '*', MUST_EXIST);
            moveto_module(get_coursemodule_from_id('', $owned->id), $section);
        }
        return (int) $owned->id;
    }

    /**
     * Remove sections past the end of the TrueNorth course once nothing is left in them.
     *
     * A section an instructor put their own activity in stays.
     *
     * @param stdClass $course
     * @param int $count number of TrueNorth sections
     */
    private static function drop_empty_trailing_sections(stdClass $course, int $count): void {
        global $DB;
        $extra = $DB->get_records_select('course_sections', 'course = ? AND section > ?',
            [$course->id, $count], 'section DESC');
        foreach ($extra as $section) {
            if (trim((string) $section->sequence) !== '') {
                break;
            }
            course_delete_section($course, $section, false);
        }
    }
}
