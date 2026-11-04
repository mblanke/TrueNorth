# Foundation F3 — real infrastructure and identity qualification

Status: blocked until F0/F1 and named test environments are available. Suggested evidence
branch: `claude/foundation-f3-real-infrastructure`.

## Closure outcome

The intended site candidate provisions, certifies, uses, snapshots, recovers and tears down
representative ranges on real writable infrastructure; Windows identity and installed Moodle
flows are proven against named third-party systems. Simulator evidence remains valuable but is
no longer presented as physical qualification.

## Environment prerequisites

- Approved non-production vCenter/ESXi cluster, networks, datastore, service account and
  cleanup authority.
- Approved Windows AD DS/AD CS environment covering direct groups, disabled/locked/expired
  accounts, LDAP/Kerberos signing policy, certificates and multi-DC behavior in scope.
- Clean installed TrueNorth candidate with optional Moodle and synthetic users/data.
- Capacity/isolation observers, evidence storage and rollback/cleanup window.

## Qualification slices

1. Register the self-hosted lab runner and prove it is pinned, isolated and manually gated.
2. Build small enterprise, cloud and OT representatives; record readiness time, allocation,
   network isolation, configuration application and certification evidence.
3. Exercise consoles, power, snapshot/restore, health, teardown, broker interruption,
   duplicate delivery, partial build and abandoned-resource reconciliation.
4. Run telemetry/noise/scenario actions against real hosts and record sensor/source limits.
5. Run external-mode Windows AD/AD CS tests and documented sign-in/MFA/account-state journeys.
6. On installed Moodle, run publication, hidden staging, SSO, Student page/quiz resume,
   completion/grade pull, republish/revision, tenant refusal and backup/restore.

## Acceptance

- Every artifact names hardware/software versions, topology, workload and time.
- Foreign-tenant credentials/resources and cross-network traffic are denied.
- Failure cleanup accounts for VMs, port groups, leases, credentials and reservations.
- No stale retry reverses newer operator intent; manual cleanup is explicit and audited.
- Windows and Moodle behavior is attributed to the tested versions/configuration only.
- Secrets, learner records and operational addresses are redacted from transferable evidence.
- Capacity results are not extrapolated beyond the tested topology.

## Recovery

Use dedicated non-production pools, snapshots/backups, resource prefixes and an independent
cleanup inventory. Stop when isolation, cleanup authority or credentials are uncertain.

## Skills

Primary: `tn-release-verification`; add `tn-job-lifecycle`, `tn-tenant-auth` and
`tn-incident-diagnosis`. Site operators execute physical steps; models cannot fabricate this
evidence. F3 supplies foundation gates for workstreams 02, 03, 05 and 07.
