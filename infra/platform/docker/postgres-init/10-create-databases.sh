#!/bin/bash
# =============================================================================
# TrueNorth Range — companion database bootstrap
# =============================================================================
# Creates the Keycloak and LRS databases and their owners.
#
# Why this exists: compose.prod.yml previously pointed Keycloak's KC_DB_URL at
# ${POSTGRES_DB}, the application's own database. That puts ~90 Keycloak tables
# alongside TrueNorth's 68, and `alembic revision --autogenerate` then proposes
# dropping every one of them. The LRS had the same problem in the dev stack.
#
# Runs only on FIRST initialisation of the data directory (Postgres semantics),
# so it is safe to leave in place. Each statement is guarded, so a partially
# initialised cluster does not abort the run.
# =============================================================================
set -euo pipefail

KEYCLOAK_DB="${KEYCLOAK_DB:-keycloak}"
KEYCLOAK_DB_USER="${KEYCLOAK_DB_USER:-keycloak}"
LRS_DB="${LRS_DB:-lrs}"
LRS_DB_USER="${LRS_DB_USER:-lrs}"

if [ -z "${KEYCLOAK_DB_PASSWORD:-}" ]; then
  echo "FATAL: KEYCLOAK_DB_PASSWORD is unset. Keycloak cannot be provisioned." >&2
  exit 1
fi
if [ -z "${LRS_DB_PASSWORD:-}" ]; then
  echo "FATAL: LRS_DB_PASSWORD is unset. The LRS cannot be provisioned." >&2
  exit 1
fi

create_role_and_db() {
  local role="$1" password="$2" db="$3"

  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	DO \$\$
	BEGIN
	  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${role}') THEN
	    CREATE ROLE ${role} LOGIN PASSWORD '${password}';
	  END IF;
	END
	\$\$;
EOSQL

  # CREATE DATABASE cannot run inside a transaction block or a DO block, so it
  # is issued separately and only when absent.
  if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${db}'" \
       --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" | grep -q 1; then
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      -c "CREATE DATABASE ${db} OWNER ${role};"
  fi

  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "GRANT ALL PRIVILEGES ON DATABASE ${db} TO ${role};"

  echo "  provisioned database '${db}' owned by '${role}'"
}

echo "TrueNorth: provisioning companion databases..."
create_role_and_db "$KEYCLOAK_DB_USER" "$KEYCLOAK_DB_PASSWORD" "$KEYCLOAK_DB"
create_role_and_db "$LRS_DB_USER"      "$LRS_DB_PASSWORD"      "$LRS_DB"
echo "TrueNorth: companion databases ready."
