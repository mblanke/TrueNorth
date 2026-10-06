<?php
// TrueNorth -> Moodle farm bootstrap (idempotent). Run inside a Moodle container:
//   php truenorth_setup.php [--print-token] [--sso-publickey=<pem file>] [--tn-login-url=<url>]
//                           [--theme-scss=<scss file>]
//
// Enables REST web services and creates the "truenorth_sync" external service
// with the core functions TrueNorth's projector uses. With --print-token it
// prints a permanent token for the site admin on stdout (dev only; slice 5
// replaces the admin with a dedicated, least-privilege service user).
//
// --sso-publickey  TrueNorth's LTI tool public key; local_truenorth's sso.php trusts
//                  tickets signed with it. Re-run after TrueNorth rotates its key.
// --tn-login-url   Send Moodle's login page to the TrueNorth app, so nobody signs in
//                  to Moodle directly. Admins keep /login/index.php?loginredirect=0.
// --theme-scss     TrueNorth's branding for the Boost theme (infra/platform/moodle/truenorth.scss).

define('CLI_SCRIPT', true);

require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/webservice/lib.php');

[$options] = cli_get_params(
    ['print-token' => false, 'sso-publickey' => '', 'tn-login-url' => '', 'theme-scss' => '', 'tenant-id' => '',
        'help' => false],
    ['h' => 'help']
);
if ($options['help']) {
    echo "Usage: php truenorth_setup.php [--print-token] [--sso-publickey=<pem file>] [--tn-login-url=<url>]"
        . " [--theme-scss=<scss file>] [--tenant-id=<TrueNorth tenant uuid>]\n";
    exit(0);
}

if ($options['sso-publickey']) {
    $pem = trim((string) @file_get_contents($options['sso-publickey']));
    if (strpos($pem, '-----BEGIN PUBLIC KEY-----') !== 0) {
        cli_error('--sso-publickey must be a PEM public key file');
    }
    set_config('ssopublickey', $pem, 'local_truenorth');
    set_config('ssoissuer', 'truenorth', 'local_truenorth');
}
// The TrueNorth tenant this Moodle belongs to. Every TrueNorth tenant's Moodle trusts the
// same TrueNorth key, so a course-sync ticket names its tenant and only that tenant's
// Moodle accepts it (local_truenorth\ticket). Without it this Moodle refuses sync calls.
if ($options['tenant-id']) {
    if (!preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/', $options['tenant-id'])) {
        cli_error('--tenant-id must be a TrueNorth tenant UUID');
    }
    set_config('tenantid', $options['tenant-id'], 'local_truenorth');
}
// TrueNorth publishes authored HTML (pages, question text) into this site; clean every
// HTML text Moodle shows, whatever its author's capabilities.
set_config('forceclean', 1);
if ($options['tn-login-url']) {
    set_config('alternateloginurl', $options['tn-login-url']);
}
if ($options['theme-scss']) {
    $scss = (string) @file_get_contents($options['theme-scss']);
    if (trim($scss) === '') {
        cli_error('--theme-scss must be a non-empty SCSS file');
    }
    if (get_config('theme_boost', 'scss') !== $scss) {
        set_config('scss', $scss, 'theme_boost');
        theme_reset_all_caches();
    }
}

const TN_SERVICE = 'truenorth_sync';
const TN_FUNCTIONS = [
    'core_webservice_get_site_info',
    'core_course_get_categories',
    'core_course_create_categories',
    'core_course_update_categories',
    'core_course_get_courses_by_field',
    'core_course_create_courses',
    'core_course_update_courses',
    'core_course_get_contents',
    'core_course_edit_section',
    'core_course_edit_module',
    'core_user_get_users_by_field',
    'core_user_create_users',
    'core_user_update_users',
    'core_cohort_create_cohorts',
    'core_cohort_add_cohort_members',
    'core_cohort_search_cohorts',
    'enrol_manual_enrol_users',
    'enrol_manual_unenrol_users',
    'core_enrol_get_enrolled_users',
    'core_grades_get_gradeitems',
    'gradereport_user_get_grade_items',
    'core_completion_get_activities_completion_status',
    'core_completion_get_course_completion_status',
];

set_config('enablewebservices', 1);
$protocols = array_filter(explode(',', (string) get_config('core', 'webserviceprotocols')));
if (!in_array('rest', $protocols, true)) {
    $protocols[] = 'rest';
    set_config('webserviceprotocols', implode(',', $protocols));
}

$ws = new webservice();
$service = $DB->get_record('external_services', ['shortname' => TN_SERVICE]);
if (!$service) {
    $id = $ws->add_external_service((object) [
        'name' => 'TrueNorth sync',
        'shortname' => TN_SERVICE,
        'enabled' => 1,
        'restrictedusers' => 0,
        'downloadfiles' => 1,
        'uploadfiles' => 1,
    ]);
    $service = $DB->get_record('external_services', ['id' => $id], '*', MUST_EXIST);
    cli_writeln("created service " . TN_SERVICE . " id=$id");
}

$missing = [];
foreach (TN_FUNCTIONS as $fn) {
    if (!$DB->record_exists('external_functions', ['name' => $fn])) {
        $missing[] = $fn;
        continue;
    }
    if (!$ws->service_function_exists($fn, $service->id)) {
        $ws->add_external_function_to_service($fn, $service->id);
    }
}
if ($missing) {
    cli_problem('functions not present in this Moodle: ' . implode(', ', $missing));
}

if ($options['print-token']) {
    $admin = get_admin();
    $token = $DB->get_field('external_tokens', 'token', [
        'externalserviceid' => $service->id,
        'userid' => $admin->id,
        'tokentype' => EXTERNAL_TOKEN_PERMANENT,
    ], IGNORE_MULTIPLE);
    if (!$token) {
        $token = \core_external\util::generate_token(
            EXTERNAL_TOKEN_PERMANENT, $service, $admin->id, context_system::instance(), 0, '', 'truenorth-dev'
        );
    }
    echo $token, PHP_EOL;
}
