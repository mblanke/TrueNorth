<?php
// This file is part of the TrueNorth Range integration for Moodle.
//
// It is free software: you can redistribute it and/or modify it under the terms of
// the GNU General Public License as published by the Free Software Foundation,
// either version 3 of the License, or (at your option) any later version.

namespace local_truenorth;

use Firebase\JWT\JWT;
use moodle_exception;

/**
 * What students did in TrueNorth's courses, for TrueNorth to record (op `pull_results`).
 *
 * TrueNorth asks with a signed sync ticket (see {@see ticket}); this answers with the
 * activity completions and quiz grades changed since TrueNorth's cursor, signed with
 * this site's LTI key, which TrueNorth already trusts for LTI launches (it fetches the
 * public half from /mod/lti/certs.php). The answer names the ticket it answers
 * (`req` = the ticket's jti), so an old answer cannot be replayed.
 *
 * Only TrueNorth's own data leaves: courses whose idnumber is a TrueNorth course UUID
 * (never a `tn-stage:` course), activities whose idnumber starts `tn:`, and the accounts
 * TrueNorth sign-in created (see {@see sso}): idnumber a TrueNorth user UUID AND username
 * `tn-<that UUID>`. The username is what ties the row to the person: a Student cannot
 * change it (and the idnumber field is locked for them, db/install.php), so nobody can
 * report work under another Student's TrueNorth id. A person is named only by that
 * TrueNorth id: no name, email or username leaves.
 *
 * Course completions are not reported: TrueNorth gives its courses no completion
 * criteria and completes an enrolment from its modules, and Moodle's completion cron
 * writes `timecompleted` earlier than the time it writes the row, which a time cursor
 * would step over.
 *
 * The cursor is the last row read, `<time>.<source>.<id>`, so paging is exact even when
 * many rows share one second. Both sources' times are the time of the write
 * (completion_info::update_state and the quiz grade calculator set `timemodified` to
 * time()). Rows newer than `settle` seconds are left for the next pull, so a write still
 * committing is not stepped over.
 *
 * @package    local_truenorth
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class results {
    /** Most rows answered per call. */
    const MAX_LIMIT = 1000;

    /** Longest life of a signed answer, in seconds. */
    const ANSWER_SECONDS = 120;

    /** Sources, in cursor order. */
    const COMPLETION = 1;
    const QUIZ_GRADE = 2;

    /** A TrueNorth UUID (user or course idnumber). */
    const UUID = '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/';

    /**
     * Rows changed since the cursor, signed for TrueNorth.
     *
     * @param string $cursor '' for the beginning, else a cursor this returned
     * @param int $limit rows wanted (1..MAX_LIMIT)
     * @param int $settle seconds to stay behind the clock (0..60)
     * @param string $requestjti the jti of the sync ticket being answered
     * @return array ['signed' => compact JWT whose `results` claim is {rows, cursor, more}]
     */
    public static function pull(string $cursor, int $limit, int $settle, string $requestjti): array {
        $limit = max(1, min(self::MAX_LIMIT, $limit));
        $settle = max(0, min(60, $settle));
        [$t, $source, $id] = self::parse_cursor($cursor);
        $upto = time() - $settle;

        $scanned = array_merge(
            self::completions($t, $source, $id, $upto, $limit + 1),
            self::quiz_grades($t, $source, $id, $upto, $limit + 1)
        );
        usort($scanned, fn($a, $b) => [$a['t'], $a['source'], $a['id']] <=> [$b['t'], $b['source'], $b['id']]);
        $more = count($scanned) > $limit;
        $scanned = array_slice($scanned, 0, $limit);

        $rows = [];
        foreach ($scanned as $r) {
            if ($row = self::row($r)) {
                $rows[] = $row;
            }
        }
        $last = end($scanned);
        $next = $last ? "{$last['t']}.{$last['source']}.{$last['id']}" : self::format_cursor($t, $source, $id);
        return ['signed' => self::sign(['rows' => $rows, 'cursor' => $next, 'more' => $more], $requestjti)];
    }

    /**
     * The cursor's parts; anything malformed reads as the beginning.
     *
     * @param string $cursor
     * @return int[] [time, source, id]
     */
    private static function parse_cursor(string $cursor): array {
        if (!preg_match('/^(\d{1,12})\.([12])\.(\d{1,18})$/', $cursor, $m)) {
            return [0, 0, 0];
        }
        return [(int) $m[1], (int) $m[2], (int) $m[3]];
    }

    /**
     * @param int $t
     * @param int $source
     * @param int $id
     * @return string
     */
    private static function format_cursor(int $t, int $source, int $id): string {
        return $t || $source || $id ? "{$t}.{$source}.{$id}" : '';
    }

    /**
     * SQL selecting the rows of one source after the cursor, keyset on (time, id).
     *
     * @param string $time the source's time column
     * @param string $idcol the source's id column
     * @param int $mine this source
     * @param int $t cursor time
     * @param int $source cursor source
     * @param int $id cursor id
     * @return array [sql, params]
     */
    private static function after(string $time, string $idcol, int $mine, int $t, int $source, int $id): array {
        if ($mine < $source) {
            return ["$time > :ct", ['ct' => $t]];  // this source's rows at second $t were all read
        }
        $after = $mine === $source ? $id : 0;
        return ["($time > :ct OR ($time = :ct2 AND $idcol > :cid))", ['ct' => $t, 'ct2' => $t, 'cid' => $after]];
    }

    /**
     * Activity completion changes on TrueNorth activities.
     *
     * @return array[]
     */
    private static function completions(int $t, int $source, int $id, int $upto, int $limit): array {
        global $DB;
        [$where, $params] = self::after('cmc.timemodified', 'cmc.id', self::COMPLETION, $t, $source, $id);
        $sql = "SELECT cmc.id, cmc.timemodified AS t, cmc.completionstate AS state, u.idnumber AS useridnumber,
                       u.username, c.idnumber AS courseidnumber, cm.idnumber AS cmidnumber, m.name AS modname
                  FROM {course_modules_completion} cmc
                  JOIN {course_modules} cm ON cm.id = cmc.coursemoduleid
                  JOIN {modules} m ON m.id = cm.module
                  JOIN {course} c ON c.id = cm.course
                  JOIN {user} u ON u.id = cmc.userid
                 WHERE $where AND cmc.timemodified <= :upto AND u.deleted = 0
                   AND " . $DB->sql_like('cm.idnumber', ':tn') . "
              ORDER BY cmc.timemodified, cmc.id";
        $out = [];
        foreach ($DB->get_records_sql($sql, $params + ['upto' => $upto, 'tn' => 'tn:%'], 0, $limit) as $r) {
            $out[] = ['source' => self::COMPLETION, 'id' => (int) $r->id, 't' => (int) $r->t, 'kind' => 'completion',
                'user' => $r->useridnumber, 'username' => $r->username, 'course' => $r->courseidnumber,
                'activity' => $r->cmidnumber, 'modname' => $r->modname, 'state' => (int) $r->state];
        }
        return $out;
    }

    /**
     * Quiz grades (Moodle's final grade per student per quiz) on TrueNorth quizzes.
     *
     * @return array[]
     */
    private static function quiz_grades(int $t, int $source, int $id, int $upto, int $limit): array {
        global $DB;
        [$where, $params] = self::after('qg.timemodified', 'qg.id', self::QUIZ_GRADE, $t, $source, $id);
        $sql = "SELECT qg.id, qg.timemodified AS t, qg.grade, q.grade AS grademax, gi.gradepass,
                       u.idnumber AS useridnumber, u.username, c.idnumber AS courseidnumber,
                       cm.idnumber AS cmidnumber,
                       (SELECT COUNT(1) FROM {quiz_attempts} qa
                         WHERE qa.quiz = q.id AND qa.userid = qg.userid AND qa.state = 'finished' AND qa.preview = 0
                       ) AS attempts
                  FROM {quiz_grades} qg
                  JOIN {quiz} q ON q.id = qg.quiz
                  JOIN {modules} m ON m.name = 'quiz'
                  JOIN {course_modules} cm ON cm.instance = q.id AND cm.module = m.id
                  JOIN {course} c ON c.id = q.course
                  JOIN {user} u ON u.id = qg.userid
             LEFT JOIN {grade_items} gi ON gi.itemtype = 'mod' AND gi.itemmodule = 'quiz'
                                       AND gi.iteminstance = q.id AND gi.itemnumber = 0
                 WHERE $where AND qg.timemodified <= :upto AND u.deleted = 0
                   AND " . $DB->sql_like('cm.idnumber', ':tn') . "
              ORDER BY qg.timemodified, qg.id";
        $out = [];
        foreach ($DB->get_records_sql($sql, $params + ['upto' => $upto, 'tn' => 'tn:%'], 0, $limit) as $r) {
            $out[] = ['source' => self::QUIZ_GRADE, 'id' => (int) $r->id, 't' => (int) $r->t, 'kind' => 'quiz_grade',
                'user' => $r->useridnumber, 'username' => $r->username, 'course' => $r->courseidnumber,
                'activity' => $r->cmidnumber, 'modname' => 'quiz', 'grade' => (float) $r->grade, 'grademax' => (float) $r->grademax,
                'gradepass' => (float) ($r->gradepass ?? 0), 'attempts' => (int) $r->attempts];
        }
        return $out;
    }

    /**
     * The row TrueNorth receives, or null when it is not TrueNorth's to see.
     *
     * @param array $r a scanned row
     * @return array|null
     */
    public static function row(array $r): ?array {
        $user = (string) $r['user'];
        if (!preg_match(self::UUID, $user) || !preg_match(self::UUID, (string) $r['course'])) {
            return null;  // not a TrueNorth account, or a staging / non-TrueNorth course
        }
        if ((string) $r['username'] !== 'tn-' . $user) {
            return null;  // an idnumber not set by TrueNorth sign-in for this account
        }
        $row = $r;
        $row['time'] = $r['t'];
        unset($row['source'], $row['id'], $row['t'], $row['username']);
        return $row;
    }

    /**
     * Sign the answer with this site's LTI key (the key TrueNorth verifies LTI launches with).
     *
     * @param array $results {rows, cursor, more}
     * @param string $requestjti the ticket answered
     * @return string compact JWT
     */
    private static function sign(array $results, string $requestjti): string {
        global $CFG;
        $key = \mod_lti\local\ltiopenid\jwks_helper::get_private_key();
        if (empty($key['key']) || empty($key['kid'])) {
            throw new moodle_exception('resultsnokey', 'local_truenorth');
        }
        $now = time();
        return JWT::encode([
            'iss' => rtrim($CFG->wwwroot, '/'),
            'aud' => get_config('local_truenorth', 'ssoissuer') ?: 'truenorth',
            'typ' => 'results',
            'tid' => (string) get_config('local_truenorth', 'tenantid'),
            'req' => $requestjti,
            'iat' => $now,
            'exp' => $now + self::ANSWER_SECONDS,
            'results' => $results,
        ], $key['key'], 'RS256', $key['kid']);
    }
}
