-- CATAPULT's databases and user (compose.catapult.yml), as its cts/init_db.sh creates them.
CREATE DATABASE IF NOT EXISTS catapult_cts;
CREATE DATABASE IF NOT EXISTS catapult_player;
CREATE USER IF NOT EXISTS 'catapult'@'%' IDENTIFIED WITH mysql_native_password BY 'quartz';
GRANT ALL PRIVILEGES ON catapult_cts.* TO 'catapult'@'%';
GRANT ALL PRIVILEGES ON catapult_player.* TO 'catapult'@'%';
FLUSH PRIVILEGES;
