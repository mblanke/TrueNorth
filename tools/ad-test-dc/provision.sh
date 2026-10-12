#!/bin/bash
# Populate a freshly provisioned test realm with TN-DC01's layout:
#
#   OU=TrueNorth
#     OU=Users            the people (accounts.tsv, ou=Users)
#     OU=Groups           TN-Platform-Admins, TN-Range-Operators, TN-Instructors,
#                         TN-Content-Authors, TN-Students, TN-Observers, plus
#                         TN-Cohort-Alpha (a member of TN-Students: the nested-group case)
#     OU=ServiceAccounts  CN=TrueNorth Keycloak Bind (svc-keycloak-bind)
#
# Runs once, offline against sam.ldb, before samba starts. Passwords come from
# /secrets/<sam> and reach samba-tool on its argv inside this container only (CN =
# "<given> <surname>", as on TN-DC01); nothing here echoes one.
set -euo pipefail

ACCOUNTS=/usr/local/share/tn-ad-test-dc/accounts.tsv
REALM_LC="$(echo "${AD_REALM:-TNLAB.TEST}" | tr '[:upper:]' '[:lower:]')"
COLLISION="${AD_COLLISION_EMAIL:-}"

st() { samba-tool "$@" >/dev/null; }

# The test realm accepts the generated passwords as they are; the complexity rule is
# kept (the generator satisfies it), the age rules would only get in the way.
st domain passwordsettings set --min-pwd-age=0 --max-pwd-age=0

st ou add "OU=TrueNorth"
st ou add "OU=Users,OU=TrueNorth"
st ou add "OU=Groups,OU=TrueNorth"
st ou add "OU=ServiceAccounts,OU=TrueNorth"

for g in TN-Platform-Admins TN-Range-Operators TN-Instructors TN-Content-Authors \
         TN-Students TN-Observers TN-Cohort-Alpha; do
  st group add "$g" --groupou="OU=Groups,OU=TrueNorth"
done
# The nested-group case: TN-Cohort-Alpha is itself a member of TN-Students.
st group addmembers TN-Students TN-Cohort-Alpha

while IFS=$'\t' read -r sam given surname ou groups flags mail; do
  case "$sam" in ''|'#'*) continue ;; esac
  case "$mail" in
    -) mail="${sam}@${REALM_LC}" ;;
    COLLISION)
      if [ -z "$COLLISION" ]; then
        echo "[ad-test-dc] AD_COLLISION_EMAIL unset: ${sam} gets its own address" >&2
        mail="${sam}@${REALM_LC}"
      else
        mail="$COLLISION"
      fi ;;
  esac
  [ -s "/secrets/$sam" ] || { echo "[ad-test-dc] no password for $sam in /secrets" >&2; exit 2; }
  st user create "$sam" "$(cat "/secrets/$sam")" \
    --userou="OU=${ou},OU=TrueNorth" \
    --given-name="$given" --surname="$surname" \
    --mail-address="$mail"
  if [ "$groups" != "-" ]; then
    IFS=',' read -ra gs <<<"$groups"
    for g in "${gs[@]}"; do st group addmembers "$g" "$sam"; done
  fi
  if [ "$flags" = "disabled" ]; then
    st user disable "$sam"
  fi
done < "$ACCOUNTS"

echo "[ad-test-dc] OUs, groups and $(grep -cv '^#' "$ACCOUNTS") accounts created"
