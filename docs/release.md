# Releasing TrueNorth

A release is a git tag `vX.Y.Z` (or `vX.Y.Z-suffix`, published as a pre-release) on a
commit of `main` that CI passed. Pushing the tag runs `.github/workflows/release.yml`. It
publishes images and evidence. **It deploys nothing.** Installation is done by the
installer (`install/`), from the release manifest. The workflow refuses a tag whose commit
is not on `main` (`git merge-base --is-ancestor`), before anything is built or signed.

The old `deploy-dev.yml` and `deploy-prod.yml` were deleted on 2026-10-08. They could not
have worked: dev triggered on a `develop` branch that does not exist, prod retagged a
`:main` image that nothing pushed, wrote `$GITHUB_OUTPUT` on the remote host, ran
migrations after `up -d`, and bound both blue/green slots to 80/443. Prod also kept the
production SSH key in a third-party action.

Per-release notes (known limitations, staging record) live in `docs/release-notes/`,
e.g. [`v1.0.0.md`](release-notes/v1.0.0.md); the change list is `CHANGELOG.md`.

## What a release produces

For each of the five services, `api`, `worker`, `web`, `scenario-engine` and
`ai-orchestrator`, release.yml:

1. builds from the tagged commit with a fresh base image (`pull: true`) and no layer
   cache, with `TN_VERSION=<tag>` as a build arg. The tag becomes the `TN_VERSION` env var
   and the `org.opencontainers.image.version` label. The `revision` label holds the commit.
   BuildKit attaches **SLSA provenance** (`mode=max`) and an SBOM as attestations.
2. pushes the image to `ghcr.io/mblanke/truenorth-<service>` **by digest only**. Nothing is
   tagged yet.
3. scans that digest with Trivy. **Blocking:** any HIGH or CRITICAL vulnerability with a
   fix available fails the release.
4. writes an SPDX SBOM of that digest with syft (`anchore/sbom-action`).
5. **signs the scanned digest** with cosign keyless and attaches the SPDX SBOM as a signed
   attestation (`cosign attest --type spdxjson`), then verifies both against the signer
   identity ("Signatures", below).
6. only then tags the digest `:<tag>`, and checks that the tag resolves to the digest it
   scanned.

The `publish` job then signs `release-manifest.json` and `SHA256SUMS` and creates the GitHub
release with these assets:

| Asset | What |
|---|---|
| `release-manifest.json` | the interface for installers: service → image digest (below) |
| `release-manifest.json.sigstore.json` | its Sigstore bundle: certificate, signature, transparency-log proof |
| `sbom-<service>.spdx.json` | SBOM per image |
| `trivy-<service>.json` | the scan that gated the image (fixable HIGH/CRITICAL; empty when clean) |
| `SHA256SUMS` | SHA-256 of every asset above (the manifest's bundle included) |
| `SHA256SUMS.sigstore.json` | the Sigstore bundle of `SHA256SUMS` |

## Signatures

Every signature is cosign **keyless**: GitHub's OIDC token proves which workflow ran, Fulcio
issues a short-lived certificate naming it, and Rekor logs the signature publicly. There is
no signing key to steal or rotate. A valid signature must name exactly:

| | |
|---|---|
| certificate identity | `https://github.com/mblanke/TrueNorth/.github/workflows/release.yml@refs/tags/<tag>` |
| OIDC issuer | `https://token.actions.githubusercontent.com` |

The installer (`install/roles/tn_release`) verifies `release-manifest.json` against that
identity, for the tag being installed, **before** it trusts any digest in it, with a cosign
binary it pins and checks by SHA-256 (`tn_cosign_version`, `tn_cosign_sha256`; the same
release as the workflow's `COSIGN_VERSION`/`COSIGN_SHA256`). A manifest that does not
verify stops `20-fetch-app`. `-e tn_release_verify_signature=false` exists only for a
release made before signing, and says so loudly.

**Air-gapped.** The bundle carries the certificate, the signature and the Rekor inclusion
proof, so verification needs no Rekor or Fulcio access, only Sigstore's trust root. Copy a
`trusted_root.json` to the control node with the release files (on a connected machine
`cosign` caches it under `~/.sigstore/root/`; or fetch it from Sigstore's TUF repository) and
pass `-e tn_release_trusted_root=<file>`: the installer then verifies with `--offline`.
*Not yet exercised against a real release: the first signed tag is the test.*

If any image fails its build or scan, no release is created. Digests of images that
passed may already be in GHCR, untagged. They are harmless and are not in any manifest.

## Cutting a release

```bash
git fetch github && git checkout github/main
# CI must be green for this commit (Actions tab, or gh run list --commit "$(git rev-parse HEAD)")
git tag -a v1.2.3 -m "TrueNorth v1.2.3"
git push github v1.2.3
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

To redo a failed release, delete the tag (`git push github :refs/tags/v1.2.3`), fix the
cause on main, and tag again. Never move a published release's tag: installs pin digests,
and the manifest must keep describing what was scanned.

The application does not report the tag yet. `control-plane/api/app/main.py` hard-codes
`APP_VERSION = "0.1.0"`. Images carry `TN_VERSION`, and the API should read it
(follow-up for the API package).

## Verifying a release

```bash
V=v1.2.3
mkdir -p "rel-$V" && cd "rel-$V"
gh release download "$V" -R mblanke/TrueNorth
ID="https://github.com/mblanke/TrueNorth/.github/workflows/release.yml@refs/tags/$V"
ISS=https://token.actions.githubusercontent.com
for f in release-manifest.json SHA256SUMS; do       # signed by release.yml at this tag
  cosign verify-blob --bundle "$f.sigstore.json" --certificate-identity "$ID" --certificate-oidc-issuer "$ISS" "$f"
done
sha256sum -c SHA256SUMS                              # assets intact
# Each image: signature and SBOM attestation
jq -r '.images[].ref' release-manifest.json | while read -r ref; do
  cosign verify "$ref" --certificate-identity "$ID" --certificate-oidc-issuer "$ISS" >/dev/null && echo "signed  $ref"
  cosign verify-attestation --type spdxjson "$ref" --certificate-identity "$ID" --certificate-oidc-issuer "$ISS" >/dev/null
done

# Every tag still points at the digest the manifest records (and that was scanned).
jq -r '.images | to_entries[] | "\(.value.tag) \(.value.digest)"' release-manifest.json |
while read -r tag digest; do
  got=$(docker buildx imagetools inspect "$tag" --format '{{json .Manifest.Digest}}' | tr -d '"')
  [ "$got" = "$digest" ] && echo "ok   $tag" || echo "DIFF $tag: $got != $digest"
done

# The gating scan found nothing fixable at HIGH/CRITICAL.
for f in trivy-*.json; do
  echo "$f: $(jq '[.Results[]?.Vulnerabilities[]?] | length' "$f") findings"
done

# Rescan today (new advisories appear after release). Use the pinned Trivy, below.
trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
  "$(jq -r .images.api.ref release-manifest.json)"

# What is inside an image
jq -r '.packages[] | "\(.name) \(.versionInfo)"' sbom-api.spdx.json | sort | head
```

## Installing by digest: `release-manifest.json`

The installer consumes it (`install/roles/tn_release`; install/README.md "Images"):
`ansible-playbook site.yml -e tn_release_version=v1.2.3`. This is the interface:

```json
{
  "schema_version": 1,
  "version": "v1.2.3",
  "git_sha": "<40-hex commit the images were built from>",
  "created": "2026-10-08T12:00:00Z",
  "images": {
    "api":             {"image": "ghcr.io/mblanke/truenorth-api", "digest": "sha256:…",
                        "ref": "ghcr.io/mblanke/truenorth-api@sha256:…",
                        "tag": "ghcr.io/mblanke/truenorth-api:v1.2.3"},
    "worker":          {…}, "web": {…}, "scenario-engine": {…}, "ai-orchestrator": {…}
  }
}
```

Rules for consumers:

- Pull and run **`ref`** (image@digest). Never `tag`: a tag can be moved, a digest cannot.
- Check `schema_version`. Fields may be added, and a breaking change bumps it.
- Verify its Sigstore bundle against the release workflow's identity ("Signatures"), then
  check it against `SHA256SUMS` from the same release, before using it.
- `git_sha` is the commit whose `infra/platform/docker/compose.prod.yml` and Alembic
  migrations match these images. Fetch the app source at that commit, not at a branch.
- Order is unchanged: datastores, then `alembic upgrade head` in the **api** image, then
  the rest (the installer's `50-stack-up` already does this).
- `compose.prod.yml` has no `build:` sections: it runs `${TN_IMAGE_API}`,
  `${TN_IMAGE_WORKER}`, `${TN_IMAGE_WEB}` and `${TN_IMAGE_AI_ORCHESTRATOR}`, which the
  installer renders from `ref`, and pins every third-party image by digest.
  `compose.build.yml` adds the builds back for a lab. The `scenario-engine` image is
  published but not run by compose (the api mounts the engine from the source at `git_sha`).

## Pins

| What | Pinned to | How it was verified |
|---|---|---|
| cosign binary | v3.1.3, linux-amd64 SHA-256 `4629c757…f7f71` (`release.yml`; the installer pins the same release for linux/darwin amd64/arm64) | `cosign_checksums.txt` of v3.1.3 verified 2026-10-08 with `cosign verify-blob --bundle cosign_checksums.txt.sigstore.json --certificate-identity keyless@projectsigstore.iam.gserviceaccount.com --certificate-oidc-issuer https://accounts.google.com`; each binary's hash matches its line |
| Trivy binary | v0.74.0, Linux-64bit tarball SHA-256 `2ae6fe3e…8be4371a` (in `ci.yml` and `release.yml`) | `trivy_0.74.0_checksums.txt` verified with cosign against `aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v0.74.0` (OIDC issuer token.actions.githubusercontent.com); the tarball hash matches its line |
| `aquasecurity/trivy-action` | `ed142fd0673e97e23eac54620cfb913e5ce36c25` (v0.36.0, 2026-04-22) | `gh api repos/aquasecurity/trivy-action/git/ref/tags/v0.36.0` → annotated tag `a9c7b0f…`, signature verified, target `ed142fd…`; the commit's signature is verified. Run with `skip-setup-trivy: true`, so the action's installer is not used |
| `anchore/sbom-action` | `3ad7283483fc7af8ff2b4ea19663c2d5ca935e26` (v0.24.2; it pins syft v1.51.1) | tag resolved via the GitHub API; commit signature verified. v0.24.3 (2026-10-02) was too new |
| other actions | full commit SHA, with a `# vX.Y.Z` comment | tag resolved via `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` (annotated tags dereferenced) |
| k6 (CI smoke) | `grafana/k6@sha256:23f22790…` (v1.8.1) | `docker buildx imagetools inspect grafana/k6:1.8.1` |

Trivy's release channel was compromised on 2026-03-19 (CVE-2026-33634: a malicious
v0.69.4, and hijacked `trivy-action`/`setup-trivy` tags). Rules when bumping any pin:

- never `@master`, `@main` or a bare major tag;
- avoid releases younger than a few weeks;
- for Trivy, verify the cosign bundle of the checksums file, then update
  `TRIVY_VERSION` and `TRIVY_SHA256` in **both** workflows.

## Docker Hub pulls in CI

GitHub-hosted runners share egress addresses, so anonymous Docker Hub pulls fail on its
rate limit (`toomanyrequests`) and on intermittent 500s. On PR #124 that failed
build-docker, integration, e2e, load-smoke, moodle, greyspace, helm-kind and the backup
drill. CI pulls Docker Hub images through Google's public cache, `mirror.gcr.io`, instead.
It needs no credentials and serves the same content by the same digests. Digest-pinned
references stay pinned and resolve to identical bytes.

| Pull path | How it uses the mirror |
|---|---|
| Docker daemon (`docker run`/`pull`/`build`, compose, kind's node image, the buildx builder image) | `.github/actions/dockerhub-mirror`, the first step after checkout: merges `registry-mirrors` into the runner's `/etc/docker/daemon.json`, restarts dockerd and fails unless `docker info` lists the mirror. dockerd falls back to Docker Hub if the mirror fails |
| `services:` (ci.yml `test-python`) | They start before any step, and a dockerd restart would stop them, so the images are named `mirror.gcr.io/library/postgres:16` and so on |
| BuildKit (`docker/setup-buildx-action`, docker-container driver), which does not read daemon.json | `buildkitd-config-inline` with `[registry."docker.io"] mirrors = ["mirror.gcr.io"]` in ci.yml `build-docker`, helm-kind and release.yml `image`. Unlike dockerd, BuildKit did not fall back to Docker Hub when the mirror was unreachable (tested locally with an unresolvable mirror host), so these builds depend on `mirror.gcr.io` being up |
| kind's node containerd (helm-kind: `deps.yaml`, the helm test image) | `infra/k8s/kind/ci-cluster.yaml` sets containerd's `config_path`. A step then writes `certs.d/docker.io/hosts.toml` in each node, with the mirror first and Docker Hub as the fallback |
| Self-hosted runner (lab.yml) | The host's dockerd is shared and is not reconfigured. The image is named on `mirror.gcr.io` |

`tests/contracts/test_ci_dockerhub_mirror.py` fails when a hosted job that runs
docker/compose/buildx/kind lacks the action, when a service image or a buildx builder
uses Docker Hub directly, or when the kind configuration drifts. A new job that pulls
images only needs `- uses: ./.github/actions/dockerhub-mirror` after its checkout.

A release builds with `pull: true` from base-image *tags*. Through the mirror, a tag can
briefly resolve to an older digest than Docker Hub's while the cache refreshes. The
release Trivy gate scans what was actually built, and the provenance attestation records
the base digest. To force a fresh base, rerun after the cache catches up. You can also
check the tag with `docker buildx imagetools inspect` against `docker.io` and
`mirror.gcr.io`.

## Image scanning in PR CI

`ci.yml` `build-docker` builds each image (no push) and scans it with the same pinned
Trivy. It prints a report of fixable HIGH/CRITICAL (never fails) and **fails the PR on a
fixable CRITICAL**. A release also blocks on HIGH, so read the report before tagging.

The Dockerfiles apply distro security updates at build time (`apt-get upgrade` /
`apk upgrade`), because base-image tags lag them. On 2026-10-08 an un-upgraded build had
fixable HIGH findings in pcre2 (all five images) and libexpat (web). After the upgrade
step, all five images scanned clean locally (arm64 builds; CI builds amd64).

When a fixable HIGH appears in a Python or npm dependency, upgrade the pin. An ignore
file (`.trivyignore`) needs its own ADR, like the pip-audit ignores (ADR 0003).
