"""The MinIO/S3 object storage helper (app/object_store.py).

Curriculum Forge keeps uploaded courseware here. The module builds a fresh ``Minio``
client per operation and reads its configuration from the environment, so the tests
substitute a recording fake for the SDK class and drive the endpoint/TLS parsing, the
bucket bootstrap and the put/get/delete round trip through the real module code. The
SDK is an external system behind this helper (ADR 0001); faking it here is the seam
the module itself provides.
"""

from __future__ import annotations

from app import object_store


class FakeResponse:
    def __init__(self):
        self.events: list[str] = []

    def read(self):
        self.events.append("read")
        return b"courseware-bytes"

    def close(self):
        self.events.append("close")

    def release_conn(self):
        self.events.append("release_conn")


def _install_fake(monkeypatch, *, exists=False):
    made: list[dict] = []
    calls: list[tuple] = []
    responses: list[FakeResponse] = []

    class FakeMinio:
        def __init__(self, endpoint, **kwargs):
            made.append({"endpoint": endpoint, **kwargs})

        def bucket_exists(self, bucket):
            calls.append(("bucket_exists", bucket))
            return exists

        def make_bucket(self, bucket):
            calls.append(("make_bucket", bucket))

        def put_object(self, bucket, key, data, length, content_type):
            calls.append(("put_object", bucket, key, data.read(), length, content_type))

        def get_object(self, bucket, key):
            calls.append(("get_object", bucket, key))
            response = FakeResponse()
            responses.append(response)
            return response

        def remove_object(self, bucket, key):
            calls.append(("remove_object", bucket, key))

    monkeypatch.setattr(object_store, "Minio", FakeMinio)
    return made, calls, responses


# ── client construction ────────────────────────────────────────────────
def test_the_endpoint_is_parsed_and_defaults_are_minioadmin(monkeypatch):
    made, _, _ = _install_fake(monkeypatch)
    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio.internal:9000")
    monkeypatch.delenv("MINIO_SECURE", raising=False)
    object_store._client()
    assert made == [
        {"endpoint": "minio.internal:9000", "access_key": "minioadmin",
         "secret_key": "minioadmin", "secure": False}
    ]


def test_a_bare_endpoint_gets_http_and_minio_secure_forces_tls(monkeypatch):
    made, _, _ = _install_fake(monkeypatch)
    monkeypatch.setenv("MINIO_ENDPOINT", "minio.internal:9000")
    monkeypatch.setenv("MINIO_SECURE", "TRUE")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "forge")
    monkeypatch.setenv("MINIO_SECRET_KEY", "forge-secret")
    object_store._client()
    assert made == [
        {"endpoint": "minio.internal:9000", "access_key": "forge",
         "secret_key": "forge-secret", "secure": True}
    ]


# ── bucket bootstrap ───────────────────────────────────────────────────
def test_a_missing_curriculum_bucket_is_created(monkeypatch):
    _, calls, _ = _install_fake(monkeypatch, exists=False)
    object_store.ensure_bucket()
    assert calls == [("bucket_exists", "curriculum"), ("make_bucket", "curriculum")]


def test_an_existing_bucket_is_left_alone(monkeypatch):
    _, calls, _ = _install_fake(monkeypatch, exists=True)
    object_store.ensure_bucket()
    assert calls == [("bucket_exists", "curriculum")]


# ── put / get / delete ─────────────────────────────────────────────────
def test_put_object_uploads_the_bytes_under_the_given_content_type(monkeypatch):
    _, calls, _ = _install_fake(monkeypatch)
    object_store.put_object("course/x.pdf", b"pdf-bytes", "application/pdf")
    assert calls[-1] == ("put_object", "curriculum", "course/x.pdf", b"pdf-bytes", 9, "application/pdf")


def test_an_empty_content_type_becomes_octet_stream(monkeypatch):
    _, calls, _ = _install_fake(monkeypatch)
    object_store.put_object("blob", b"", "")
    assert calls[-1] == ("put_object", "curriculum", "blob", b"", 0, "application/octet-stream")


def test_get_object_returns_the_bytes_and_releases_the_connection(monkeypatch):
    _, calls, responses = _install_fake(monkeypatch)
    assert object_store.get_object("course/x.pdf") == b"courseware-bytes"
    assert calls == [("get_object", "curriculum", "course/x.pdf")]
    assert responses[0].events == ["read", "close", "release_conn"]


def test_delete_object_removes_from_the_named_bucket(monkeypatch):
    _, calls, _ = _install_fake(monkeypatch)
    object_store.delete_object("course/old.pdf", bucket="curriculum")
    assert calls == [("remove_object", "curriculum", "course/old.pdf")]
