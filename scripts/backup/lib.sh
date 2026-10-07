#!/usr/bin/env bash
# Shared by backup.sh and restore.sh: reach the stack's containers through Docker Compose
# and read credentials from the env file the installer renders.
#
# Containers are addressed by compose service (postgres, redis, ...), never by a fixed
# container name: the names the scripts used to hard-code (truenorth-postgres, user
# truenorth, database truenorth_range) existed in no deployment, so every backup failed.

# COMPOSE_FILE and ENV_FILE come from the caller (the installer's backup.env).
dc() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

# One value from the env file, read rather than sourced: the file holds JSON values with
# spaces (REGISTRATION_GROUP_ROLE_MAP) that a shell would try to execute.
envval() { grep -m1 "^$1=" "${ENV_FILE}" | cut -d= -f2-; }

# The id of a service's running container.
cid() { dc ps -q "$1"; }

# The compose network the datastores are on (tn-backend in compose.prod.yml).
BACKEND_NETWORK="${BACKEND_NETWORK:-tn-backend}"
# Pinned, so a backup does not depend on what `latest` means today.
MC_IMAGE="${MC_IMAGE:-minio/mc:RELEASE.2024-06-12T14-34-03Z}"
