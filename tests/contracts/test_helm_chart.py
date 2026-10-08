"""Contract: the Helm chart (infra/k8s/helm/truenorth-range) keeps its production posture.

Helm is a supported install target for v1.0.0. These tests render the chart with
``helm template`` (skipped where helm is not installed) and assert on the manifests, not
on template text: no default secrets, a hardened securityContext on every container,
migrations as a hook Job rather than an init container, nothing but the web app, the API
and Keycloak on the ingress, worker probes that ping the workers' real node names, and
the API's health probe paths. ``infra/k8s/kind/smoke.sh`` (CI: helm-kind.yml) installs
the same chart into a cluster.

Also covers infra/k8s/scripts/values_from_manifest.py, which pins the images to a
release's digests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "infra/k8s/helm/truenorth-range"
KIND_VALUES = REPO / "infra/k8s/kind/values-kind.yaml"
HELM = shutil.which("helm")

needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")

SECRETS = {
    "databasePassword": "d" * 32,
    "redisPassword": "r" * 32,
    "minioAccessKey": "tn-access",
    "minioSecretKey": "m" * 32,
    "csrfSecret": "c" * 32,
    "secretsKey": "s" * 32,
    "aiServiceToken": "a" * 32,
    "metricsScrapeToken": "t" * 32,
}
WORKERS = {"worker-provision": "provision", "worker-scenario": "scenario", "worker-telemetry": "telemetry"}


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [HELM, "template", "tn", str(CHART), "-n", "truenorth", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _secret_args(secrets: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, value in secrets.items():
        out += ["--set-string", f"secrets.{key}={value}"]
    return out


def render(*extra: str, secrets: dict[str, str] | None = None, kind_values: bool = True) -> list[dict]:
    args = (["-f", str(KIND_VALUES)] if kind_values else []) + _secret_args(SECRETS if secrets is None else secrets)
    proc = _helm(*args, *extra)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def render_error(*extra: str, secrets: dict[str, str] | None = None) -> str:
    proc = _helm("-f", str(KIND_VALUES), *_secret_args(SECRETS if secrets is None else secrets), *extra)
    assert proc.returncode != 0, "render was expected to fail"
    return proc.stderr


def pod_specs(docs: list[dict]) -> dict[str, dict]:
    """name -> pod spec, for every Deployment, Job and Pod."""
    out = {}
    for d in docs:
        if d["kind"] in ("Deployment", "Job"):
            out[f"{d['kind']}/{d['metadata']['name']}"] = d["spec"]["template"]["spec"]
        elif d["kind"] == "Pod":
            out[f"Pod/{d['metadata']['name']}"] = d["spec"]
    return out


def deployment(docs: list[dict], component: str) -> dict:
    return next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["labels"]["app.kubernetes.io/component"] == component
    )


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    if HELM is None:
        pytest.skip("helm is not installed")
    return render()


# -- secrets ---------------------------------------------------------------


DEFAULT_SECRETS = ("changeme", "change-me", "minioadmin")


def test_values_ship_no_default_secrets():
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    flat = json.dumps(values).lower()
    for word in DEFAULT_SECRETS:
        assert word not in flat, word
    assert all(v in ("", {}) for v in values["secrets"].values()), values["secrets"]


@needs_helm
def test_rendered_release_carries_no_default_secret(docs):
    import base64

    for d in docs:
        text = json.dumps(d).lower()
        if d["kind"] == "Secret":
            text += " ".join(base64.b64decode(v).decode().lower() for v in d.get("data", {}).values())
        for word in DEFAULT_SECRETS:
            assert word not in text, (d["kind"], d["metadata"]["name"], word)


@needs_helm
def test_render_without_secrets_fails_naming_the_setting():
    proc = _helm("-f", str(KIND_VALUES))
    assert proc.returncode != 0
    assert "secrets." in proc.stderr and "required" in proc.stderr


@needs_helm
@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("databasePassword", "changeme", "known default"),
        ("minioSecretKey", "minioadmin", "known default"),
        ("minioAccessKey", "minioadmin", "known default"),
        ("csrfSecret", "short", "shorter than 32"),
        ("secretsKey", "x" * 31, "shorter than 32"),
    ],
)
def test_weak_secrets_are_refused(key, value, message):
    assert message in render_error(secrets={**SECRETS, key: value})


@needs_helm
def test_a_secret_yaml_read_as_a_number_is_refused(tmp_path):
    # An unquoted hex-looking value in a values file is a YAML float (1.2345e+36).
    values = tmp_path / "secrets.yaml"
    values.write_text("secrets:\n  aiServiceToken: 12345678901234567890123456789012e5\n")
    others = {k: v for k, v in SECRETS.items() if k != "aiServiceToken"}
    err = render_error("-f", str(values), secrets=others)
    assert "must be a quoted string" in err


@needs_helm
def test_every_required_secret_is_in_the_secret(docs):
    secret = next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-secret"))
    for key in (
        "DATABASE_PASSWORD",
        "REDIS_PASSWORD",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "CSRF_SECRET",
        "TN_SECRETS_KEY",
        "AI_SERVICE_TOKEN",
        "METRICS_SCRAPE_TOKEN",
    ):
        assert secret["data"].get(key), key


@needs_helm
def test_existing_secret_replaces_the_charts_secret():
    docs = render("--set", "secrets.existingSecret=tn-prod", secrets={})
    assert not [d for d in docs if d["kind"] == "Secret"]
    api = deployment(docs, "api")["spec"]["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "tn-prod"}} in api["envFrom"]
    job = next(d for d in docs if d["kind"] == "Job")
    env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DATABASE_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "tn-prod"


# -- securityContext and resources -------------------------------------------


@needs_helm
def test_every_container_is_hardened_and_bounded(docs):
    specs = pod_specs(docs)
    assert len(specs) >= 9  # api, 3 workers, beat, ai, web, migrate Job, test Pod
    for name, spec in specs.items():
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True, name
        assert pod["seccompProfile"]["type"] == "RuntimeDefault", name
        assert "fsGroup" in pod, name
        assert spec["automountServiceAccountToken"] is False, name
        for c in spec.get("initContainers", []) + spec["containers"]:
            sc = c["securityContext"]
            where = f"{name}/{c['name']}"
            assert sc["runAsNonRoot"] is True, where
            assert sc["runAsUser"] in (10001, 101, 100), where
            assert sc["readOnlyRootFilesystem"] is True, where
            assert sc["allowPrivilegeEscalation"] is False, where
            assert sc["capabilities"]["drop"] == ["ALL"], where
            assert sc["seccompProfile"]["type"] == "RuntimeDefault", where
            for kind in ("requests", "limits"):
                assert {"cpu", "memory"} <= set(c["resources"][kind]), f"{where} {kind}"


@needs_helm
def test_web_runs_as_the_nginx_user(docs):
    web = deployment(docs, "web")["spec"]["template"]["spec"]
    assert web["securityContext"]["runAsUser"] == 101
    mounts = {m["mountPath"] for m in web["containers"][0]["volumeMounts"]}
    assert {"/tmp", "/var/cache/nginx"} <= mounts


# -- migrations --------------------------------------------------------------


@needs_helm
def test_migrations_are_a_hook_job_not_an_init_container(docs):
    jobs = [d for d in docs if d["kind"] == "Job"]
    assert len(jobs) == 1
    job = jobs[0]
    ann = job["metadata"]["annotations"]
    assert set(ann["helm.sh/hook"].split(",")) == {"pre-install", "pre-upgrade"}
    assert "helm.sh/hook-weight" in ann
    assert job["spec"]["backoffLimit"] >= 1
    assert job["spec"]["ttlSecondsAfterFinished"] > 0
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["alembic", "upgrade", "head"]
    # Hooks run before the release's ServiceAccount exists.
    assert "serviceAccountName" not in job["spec"]["template"]["spec"]
    for d in docs:
        if d["kind"] == "Deployment":
            for c in d["spec"]["template"]["spec"].get("initContainers", []):
                assert "alembic" not in json.dumps(c), d["metadata"]["name"]


@needs_helm
def test_the_migration_jobs_password_exists_before_the_release(docs):
    """Pre-install: the release's Secret does not exist yet, so the Job reads a hook Secret."""
    job = next(d for d in docs if d["kind"] == "Job")
    env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["DATABASE_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"]
    hook_secret = next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == ref)
    assert "pre-install" in hook_secret["metadata"]["annotations"]["helm.sh/hook"]
    assert int(hook_secret["metadata"]["annotations"]["helm.sh/hook-weight"]) < int(
        job["metadata"]["annotations"]["helm.sh/hook-weight"]
    )


# -- exposure ----------------------------------------------------------------


@needs_helm
def test_ingress_exposes_only_web_api_and_keycloak():
    docs = render("--set", "ingress.keycloak.enabled=true", "--set", "ingress.keycloak.serviceName=keycloak")
    backends, paths = set(), []
    for ing in (d for d in docs if d["kind"] == "Ingress"):
        for rule in ing["spec"]["rules"]:
            for p in rule["http"]["paths"]:
                paths.append(p["path"])
                backends.add(p["backend"]["service"]["name"])
    assert backends == {"tn-truenorth-range-api", "tn-truenorth-range-web", "keycloak"}
    assert not [p for p in paths if p.startswith(("/ai", "/flower"))], paths
    assert "/auth" in paths


@needs_helm
def test_api_ingress_strips_the_api_prefix(docs):
    api = next(d for d in docs if d["kind"] == "Ingress" and d["metadata"]["name"].endswith("-api"))
    assert api["metadata"]["annotations"]["nginx.ingress.kubernetes.io/rewrite-target"] == "/$2"
    rewrite = api["metadata"]["annotations"]["nginx.ingress.kubernetes.io/rewrite-target"].replace("$2", r"\2")
    for p in api["spec"]["rules"][0]["http"]["paths"]:
        rx = re.compile("^" + p["path"])
        if p["path"].startswith("/api"):
            assert rx.sub(rewrite, "/api/users/me") == "/users/me"
        else:
            assert rx.sub(rewrite, "/ws/tenant") == "/ws/tenant"


@needs_helm
def test_flower_is_off_and_never_exposed_when_on():
    assert not [d for d in render() if d["metadata"]["name"].endswith("-flower")]
    docs = render("--set", "flower.enabled=true", secrets={**SECRETS, "flowerBasicAuth": "ops:" + "f" * 24})
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"].endswith("-flower"))
    assert svc["spec"]["type"] == "ClusterIP"
    pol = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-flower"))
    assert "Ingress" in pol["spec"]["policyTypes"] and not pol["spec"].get("ingress")
    assert "flower" not in json.dumps([d for d in docs if d["kind"] == "Ingress"])


@needs_helm
def test_ai_orchestrator_admits_only_api_and_workers(docs):
    pol = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-ai-orchestrator"))
    (rule,) = pol["spec"]["ingress"]
    (peer,) = rule["from"]
    assert "namespaceSelector" not in peer
    (expr,) = peer["podSelector"]["matchExpressions"]
    assert set(expr["values"]) == {"api", *WORKERS}


# -- probes ------------------------------------------------------------------


@needs_helm
def test_worker_liveness_pings_the_workers_own_node_name(docs):
    for component, node in WORKERS.items():
        c = deployment(docs, component)["spec"]["template"]["spec"]["containers"][0]
        n = c["command"][c["command"].index("-n") + 1]
        assert n == f"{node}@%h"
        probe = " ".join(c["livenessProbe"]["exec"]["command"])
        assert f"-d {node}@$HOSTNAME" in probe
        assert "celery@" not in probe


@needs_helm
def test_api_probes(docs):
    c = deployment(docs, "api")["spec"]["template"]["spec"]["containers"][0]
    assert c["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert c["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert c["startupProbe"]["httpGet"]["path"] == "/health/live"


@needs_helm
def test_beat_is_a_single_recreated_replica(docs):
    beat = deployment(docs, "beat")
    assert beat["spec"]["replicas"] == 1
    assert beat["spec"]["strategy"]["type"] == "Recreate"
    assert "beat" in beat["spec"]["template"]["spec"]["containers"][0]["command"]


# -- environment ---------------------------------------------------------------


@needs_helm
def test_production_environment_is_carried(docs):
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config"))["data"]
    assert cm["TN_ENV"] == "production"
    assert cm["LOG_FORMAT"] == "json"
    for key in ("TN_VERSION", "TRUSTED_PROXY_CIDRS", "KEYCLOAK_AUDIENCE", "SCHEDULER_FEED_BASE_URL", "OPENSEARCH_URL"):
        assert cm.get(key), key
    assert cm["DB_AUTO_CREATE"] == "false" and cm["SEED_DEV_DATA"] == "false"
    ai = deployment(docs, "ai-orchestrator")["spec"]["template"]["spec"]["containers"][0]
    names = {e["name"] for e in ai["env"]}
    assert {"AI_SERVICE_TOKEN", "TN_ENV", "TN_VERSION", "LOG_FORMAT"} <= names
    assert "envFrom" not in ai  # only what it uses, not the whole Secret
    for component in ("api", *WORKERS, "beat"):
        c = deployment(docs, component)["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value", "") for e in c["env"]}
        assert env["DATABASE_URL"].startswith("postgresql+psycopg://"), component
        assert {"configMapRef": {"name": "tn-truenorth-range-config"}} in c["envFrom"], component


@needs_helm
def test_opensearch_must_be_tls_unless_security_is_disabled():
    err = render_error(
        "--set",
        "opensearch.securityDisabled=false",
        "--set",
        "opensearch.url=http://os:9200",
        "--set-string",
        "secrets.opensearchPassword=" + "o" * 32,
    )
    assert "must be https://" in err


# -- images --------------------------------------------------------------------


@needs_helm
def test_images_pin_by_digest():
    digest = "sha256:" + "ab" * 32
    docs = render(
        "--set", f"images.api.digest={digest}", "--set", "images.api.repository=ghcr.io/mblanke/truenorth-api"
    )
    c = deployment(docs, "api")["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == f"ghcr.io/mblanke/truenorth-api@{digest}"


@needs_helm
def test_a_malformed_digest_is_refused():
    assert "must be sha256:<64 hex>" in render_error("--set", "images.web.digest=sha256:nothex")


@needs_helm
def test_default_values_lint():
    proc = subprocess.run(  # noqa: S603
        [HELM, "lint", str(CHART)], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# -- values_from_manifest.py ----------------------------------------------------


def _load_script():
    path = REPO / "infra/k8s/scripts/values_from_manifest.py"
    spec = importlib.util.spec_from_file_location("tn_values_from_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _manifest() -> dict:
    services = ["api", "worker", "web", "scenario-engine", "ai-orchestrator"]
    images = {}
    for i, s in enumerate(services):
        image, digest = f"ghcr.io/mblanke/truenorth-{s}", "sha256:" + f"{i:x}" * 64
        images[s] = {"image": image, "digest": digest, "ref": f"{image}@{digest}", "tag": f"{image}:v1.2.3"}
    return {"schema_version": 1, "version": "v1.2.3", "git_sha": "0" * 40, "images": images}


def test_manifest_to_values(tmp_path, capsys):
    mod = _load_script()
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps(_manifest()))
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  release-manifest.json\n")
    assert mod.main([str(manifest), "--sums", str(sums)]) == 0
    values = yaml.safe_load(capsys.readouterr().out)
    assert values["version"] == "v1.2.3"
    assert set(values["images"]) == {"api", "worker", "web", "scenarioEngine", "aiOrchestrator"}
    assert values["images"]["scenarioEngine"]["repository"] == "ghcr.io/mblanke/truenorth-scenario-engine"
    assert values["images"]["api"]["digest"] == "sha256:" + "0" * 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(schema_version=2),
        lambda m: m["images"].pop("worker"),
        lambda m: m["images"]["web"].update(digest="sha256:short"),
        lambda m: m["images"]["api"].update(ref="ghcr.io/evil/api@sha256:" + "0" * 64),
        lambda m: m.update(version="latest"),
    ],
)
def test_manifest_problems_are_refused(tmp_path, mutate):
    mod = _load_script()
    m = _manifest()
    mutate(m)
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(m))
    assert mod.main([str(path)]) == 1


def test_manifest_checksum_mismatch_is_refused(tmp_path):
    mod = _load_script()
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest()))
    (tmp_path / "SHA256SUMS").write_text("0" * 64 + "  release-manifest.json\n")
    assert mod.main([str(path), "--sums", str(tmp_path / "SHA256SUMS")]) == 1


@needs_helm
def test_generated_values_render(tmp_path, capsys):
    mod = _load_script()
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps(_manifest()))
    assert mod.main([str(manifest)]) == 0
    values = tmp_path / "images.yaml"
    values.write_text(capsys.readouterr().out)
    docs = render("-f", str(values))
    images = {
        c["image"]
        for spec in pod_specs(docs).values()
        for c in spec.get("initContainers", []) + spec["containers"]
        if "truenorth" in c["image"]
    }
    assert images and all("@sha256:" in i for i in images), images
