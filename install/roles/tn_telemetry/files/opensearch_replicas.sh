# Single-node OpenSearch: no index may ask for a replica (roles/tn_telemetry/tasks/replicas.yml).
#
# Runs inside the opensearch container (docker compose exec -T opensearch sh -s), from its
# working directory /usr/share/opensearch. Authenticates with the admin client certificate
# (CN=tn-opensearch-admin, plugins.security.authcz.admin_dn): no password is involved, and
# system indices are writable. Plain http when the security plugin is disabled (lab override).
#
# Prints one line: "unchanged", "multi-node <n>", or "changed: <what>".
set -eu

if [ "${DISABLE_SECURITY_PLUGIN:-false}" = true ]; then
  base=http://localhost:9200
  os() { curl -sS --fail "$@"; }
else
  base=https://localhost:9200
  os() {
    curl -sS --fail --cacert config/certs/ca.pem \
      --cert config/certs/admin.pem --key config/certs/admin-key.pem "$@"
  }
fi

# Only a lone node: on a real cluster replicas are wanted and can be assigned.
# (Each answer is captured before it is parsed: a failed request in a pipeline would not
# stop the script under set -e.)
listing=$(os "$base/_cat/nodes?h=name")
nodes=$(printf '%s\n' "$listing" | grep -c . || true)
if [ "$nodes" -ne 1 ]; then
  echo "multi-node $nodes"
  exit 0
fi

changed=""

# 1. New indices. OpenSearch 2.13 accepts cluster.default_number_of_replicas as a node
#    setting (compose.prod.yml) but index creation ignores it there (verified 2026-10-15:
#    a plain PUT /<index> still got 1 replica). As a persistent cluster setting it applies
#    to every new index, hidden and plugin ones included (.opendistro-job-scheduler-lock,
#    which the first ISM job run creates lazily, possibly after this has run).
current=$(os "$base/_cluster/settings?flat_settings=true&filter_path=persistent")
case "$current" in
  *'"cluster.default_number_of_replicas":"0"'*) ;;
  *)
    os -X PUT "$base/_cluster/settings" -H 'Content-Type: application/json' \
      -d '{"persistent":{"cluster.default_number_of_replicas":0}}' >/dev/null
    changed="$changed cluster.default_number_of_replicas=0"
    ;;
esac

# 2. Existing indices, hidden and system ones included, that still ask for a replica.
listing=$(os "$base/_cat/indices?h=index,rep&expand_wildcards=all")
for index in $(printf '%s\n' "$listing" | awk '$2 > 0 {print $1}'); do
  os -X PUT "$base/$index/_settings?expand_wildcards=all" -H 'Content-Type: application/json' \
    -d '{"index":{"number_of_replicas":0}}' >/dev/null
  changed="$changed $index"
done

if [ -n "$changed" ]; then
  echo "changed:$changed"
else
  echo "unchanged"
fi
