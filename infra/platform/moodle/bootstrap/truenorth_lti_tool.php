<?php
// Register TrueNorth as a site-wide LTI 1.3 External tool (idempotent).
//   php truenorth_lti_tool.php --tool=<browser URL of the TN API> --publickey=<PEM file>
//
// --tool       what the student's browser reaches, e.g. http://localhost:8081
// --publickey  TrueNorth's LTI tool public key (PEM). Moodle verifies TrueNorth's
//              signatures with it and never calls TrueNorth. A keyset URL would need
//              Moodle to fetch from a private address, which Moodle's default
//              curlsecurityblockedhosts refuses. Re-run this after a key rotation.
// Prints JSON with the values TrueNorth's ExternalPlatform registration needs.

define('CLI_SCRIPT', true);

require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/mod/lti/locallib.php');

[$options] = cli_get_params(['tool' => '', 'publickey' => '', 'name' => 'TrueNorth Range', 'help' => false]);
if ($options['help'] || !$options['tool'] || !$options['publickey']) {
    echo "Usage: php truenorth_lti_tool.php --tool=<url> --publickey=<pem file> [--name=<name>]\n";
    exit($options['help'] ? 0 : 1);
}

$tool = rtrim($options['tool'], '/');
$launch = "$tool/lti/launch";
$publickey = trim((string) @file_get_contents($options['publickey']));
if (strpos($publickey, '-----BEGIN PUBLIC KEY-----') !== 0) {
    cli_error('--publickey must be a PEM public key file');
}

$config = (object) [
    'lti_typename' => $options['name'],
    'lti_toolurl' => $launch,
    'lti_description' => 'TrueNorth Range: cyber ranges and TrueNorth-hosted course content',
    'lti_ltiversion' => LTI_VERSION_1P3,
    'lti_keytype' => LTI_RSA_KEY,
    'lti_publickey' => $publickey,
    'lti_initiatelogin' => "$tool/lti/login",
    'lti_redirectionuris' => $launch,
    'lti_contentitem' => 1,
    'lti_toolurl_ContentItemSelectionRequest' => $launch,
    'lti_coursevisible' => LTI_COURSEVISIBLE_ACTIVITYCHOOSER,
    'lti_launchcontainer' => LTI_LAUNCH_CONTAINER_WINDOW,
    'lti_sendname' => LTI_SETTING_ALWAYS,
    'lti_sendemailaddr' => LTI_SETTING_ALWAYS,
    'lti_acceptgrades' => LTI_SETTING_ALWAYS,
    'lti_forcessl' => 0,
    'ltiservice_gradesynchronization' => 2, // Use AGS for grade sync and column management.
    'ltiservice_memberships' => 1,
    'ltiservice_toolsettings' => 0,
];

$existing = $DB->get_record('lti_types', ['name' => $options['name'], 'course' => SITEID]);
if ($existing) {
    $type = (object) ['id' => $existing->id, 'state' => LTI_TOOL_STATE_CONFIGURED, 'clientid' => $existing->clientid];
    lti_update_type($type, $config);
    $typeid = $existing->id;
} else {
    $type = (object) ['state' => LTI_TOOL_STATE_CONFIGURED, 'course' => SITEID];
    $typeid = lti_add_type($type, $config);
}
$row = $DB->get_record('lti_types', ['id' => $typeid], '*', MUST_EXIST);

// Moodle uses the tool type id as the LTI deployment id.
echo json_encode([
    'lti_type_id' => (int) $row->id,
    'lti_issuer' => $CFG->wwwroot,
    'lti_client_id' => $row->clientid,
    'lti_deployment_id' => (string) $row->id,
    'lti_auth_login_url' => $CFG->wwwroot . '/mod/lti/auth.php',
    'lti_token_url' => $CFG->wwwroot . '/mod/lti/token.php',
    'lti_jwks_url' => $CFG->wwwroot . '/mod/lti/certs.php',
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), PHP_EOL;
