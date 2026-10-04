<?php
// Run inside the local Moodle test container, alongside truenorth.scss.
define('CLI_SCRIPT', true);
require('/var/www/html/config.php');
require_once($CFG->libdir . '/adminlib.php');
require_once($CFG->dirroot . '/course/lib.php');
$scss = file_get_contents(__DIR__ . '/truenorth.scss');
if ($scss === false) {
    throw new RuntimeException('Missing TrueNorth stylesheet');
}
set_config('brandcolor', '#c8102e', 'theme_boost');
set_config('scss', $scss, 'theme_boost');
set_config('custommenuitems', 'TrueNorth|http://localhost:4200/learning');
foreach (['fullname' => 'TrueNorth Learning', 'shortname' => 'TrueNorth'] as $name => $value) {
    $setting = new admin_setting_sitesettext($name, $name, '', '', PARAM_TEXT);
    $error = $setting->write_setting($value);
    if ($error !== '') {
        throw new RuntimeException($error);
    }
}
purge_all_caches();
echo "TrueNorth test branding applied.\n";
