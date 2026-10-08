# Releasing TrueNorth

A release is a git tag `vX.Y.Z` (or `vX.Y.Z-suffix`, published as a pre-release) on a
commit of `main` that CI passed. Pushing the tag runs `.github/workflows/release.yml`. It
publishes images and evidence. **It deploys nothing.** Installation is done by the
installer (`install/`), from the release manifest.

The old `deploy-dev.yml` and `deploy-prod.yml` were deleted on 2026-10-08. They could not
have worked: dev triggered on a `develop` branch that does not exist, prod retagged a
`:main` image that nothing pushed, wrote `$GITHUB_OUTPUT` on the remote host, ran
migrations after `up -d`, and bound both blue/green slots to 80/443. Prod also kept the
production SSH key in a third-party action.

## What a release produces

For each of the five services, `api`, `worker`, `web`, `scenario-engine` and
`ai-orchestrator`, release.yml:

1. builds from the tagged commit with a fresh base image (`pull: true`) and no layer
   cache, with `TN_VERSION=<tag>` as a build arg. The tag becomes the `TN_VERSION` env var
   and the `org.opencontainers.image.version` label. The `revision` label holds the commit.
2. pushes the image to `ghcr.io/mblanke/truenorth-<service>` **by digest only**. Nothing is
   tagged yet.
3. scans that digest with Trivy. **Blocking:** any HIGH or CRITICAL vulnerability with a
   fix available fails the release.
4. writes an SPDX SBOM of that digest with syft (`anchore/sbom-action`).
5. only then tags the digest `:<tag>`, and checks that the tag resolves to the digest it
   scanned.

The `publish` job then creates the GitHub release with these assets:

| Asset | What |
|---|---|
| `release-manifest.json` | the interface for installers: service → image digest (below) |
| `sbom-<service>.spdx.json` | SBOM per image |
| `trivy-<service>.json` | the scan that gated the image (fixable HIGH/CRITICAL; empty when clean) |
| `SHA256SUMS` | SHA-256 of every asset above |

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
sha256sum -c SHA256SUMS                              # assets intact

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

The installer change is a separate work package. This is the interface it consumes:

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
- Check the file against `SHA256SUMS` from the same release before using it.
- `git_sha` is the commit whose `infra/platform/docker/compose.prod.yml` and Alembic
  migrations match these images. Fetch the app source at that commit, not at a branch.
- Order is unchanged: datastores, then `alembic upgrade head` in the **api** image, then
  the rest (the installer's `50-stack-up` already does this).
- `compose.prod.yml` still has `build:` sections and ignores `IMAGE_TAG`. The installer
  package has to render or override `image: <ref>` per service, and must not build on the
  target.

## Pins

| What | Pinned to | How it was verified |
|---|---|---|
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
