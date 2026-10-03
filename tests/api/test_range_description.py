"""Range description + supporting documents.

A range could not say what it was for. `description` is operator-facing markdown
that stays editable and searchable; documents keep their original bytes so a
briefing pack can be handed back out intact.
"""

import io

import pytest

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def rng(db_session):
    from app.models import Range, Template

    tpl = Template(name="desc-tpl", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    r = Range(name="Documented Range", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(r)
    db_session.commit()
    return r


class TestDescription:
    def test_defaults_to_empty_and_round_trips_through_update(self, client, rng):
        assert client.get(f"/ranges/{rng.id}").json()["description"] == ""

        text = "# Purpose\n\nAD lab for DP2 detection drills.\n\n## ROE\nNo egress."
        resp = client.put(f"/ranges/{rng.id}", json={"description": text})
        assert resp.status_code == 200
        assert resp.json()["description"] == text
        assert client.get(f"/ranges/{rng.id}").json()["description"] == text

    def test_updating_description_leaves_the_name_alone(self, client, rng):
        client.put(f"/ranges/{rng.id}", json={"description": "just the description"})
        assert client.get(f"/ranges/{rng.id}").json()["name"] == "Documented Range"

    def test_description_appears_in_the_list(self, client, rng):
        client.put(f"/ranges/{rng.id}", json={"description": "listed"})
        row = next(r for r in client.get("/ranges").json() if r["id"] == str(rng.id))
        assert row["description"] == "listed"

    def test_import_from_a_markdown_file_replaces_it(self, client, rng):
        body = "# Imported\n\nFrom a file.\n"
        resp = client.post(
            f"/ranges/{rng.id}/description/import",
            files={"file": ("range.md", io.BytesIO(body.encode()), "text/markdown")},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == body

    def test_import_strips_a_utf8_bom(self, client, rng):
        resp = client.post(
            f"/ranges/{rng.id}/description/import",
            files={"file": ("range.txt", io.BytesIO("\ufeffNo BOM here".encode()), "text/plain")},
        )
        assert resp.json()["description"] == "No BOM here"

    def test_non_text_file_is_rejected(self, client, rng):
        resp = client.post(
            f"/ranges/{rng.id}/description/import",
            files={"file": ("topology.png", io.BytesIO(b"\x89PNG..."), "image/png")},
        )
        assert resp.status_code == 415

    def test_empty_and_undecodable_files_are_rejected(self, client, rng):
        empty = client.post(
            f"/ranges/{rng.id}/description/import",
            files={"file": ("range.md", io.BytesIO(b""), "text/markdown")},
        )
        assert empty.status_code == 422
        binary = client.post(
            f"/ranges/{rng.id}/description/import",
            files={"file": ("range.txt", io.BytesIO(b"\xff\xfe\x00\x01binary"), "text/plain")},
        )
        assert binary.status_code == 422

    def test_another_tenants_range_is_not_reachable(self, client, db_session):
        import uuid as _uuid

        from app.models import Range, Template

        other = _uuid.uuid4()
        tpl = Template(name="foreign", version="1.0", yaml="nodes: []", tenant_id=other)
        db_session.add(tpl)
        db_session.flush()
        r = Range(name="Foreign", template_id=tpl.id, tenant_id=other, state="ready")
        db_session.add(r)
        db_session.commit()
        assert client.put(f"/ranges/{r.id}", json={"description": "mine now"}).status_code == 404


class TestDocuments:
    """Object storage is stubbed: these pin the wiring, not MinIO itself."""

    @pytest.fixture(autouse=True)
    def fake_store(self, monkeypatch):
        from app import object_store

        store: dict[str, bytes] = {}
        monkeypatch.setattr(object_store, "put_object", lambda k, d, c, bucket=None: store.__setitem__(k, d))
        monkeypatch.setattr(object_store, "get_object", lambda k, bucket=None: store[k])
        monkeypatch.setattr(object_store, "delete_object", lambda k, bucket=None: store.pop(k, None))
        return store

    def test_upload_list_download_delete(self, client, rng, fake_store):
        up = client.post(
            f"/ranges/{rng.id}/documents",
            files=[("files", ("roe.pdf", io.BytesIO(b"%PDF-1.4 rules"), "application/pdf"))],
        )
        assert up.status_code == 201
        doc = up.json()[0]
        assert doc["filename"] == "roe.pdf"
        assert doc["size_bytes"] == len(b"%PDF-1.4 rules")

        listing = client.get(f"/ranges/{rng.id}/documents").json()
        assert [d["id"] for d in listing] == [doc["id"]]

        got = client.get(f"/ranges/{rng.id}/documents/{doc['id']}")
        assert got.status_code == 200
        assert got.content == b"%PDF-1.4 rules"
        assert "roe.pdf" in got.headers["content-disposition"]

        assert client.delete(f"/ranges/{rng.id}/documents/{doc['id']}").status_code == 204
        assert client.get(f"/ranges/{rng.id}/documents").json() == []

    def test_empty_upload_is_rejected(self, client, rng):
        resp = client.post(
            f"/ranges/{rng.id}/documents",
            files=[("files", ("blank.txt", io.BytesIO(b""), "text/plain"))],
        )
        assert resp.status_code == 422

    def test_document_of_another_range_is_404(self, client, rng, db_session):
        from app.models import Range

        other = Range(name="Other", template_id=rng.template_id, tenant_id=DEV_TENANT, state="ready")
        db_session.add(other)
        db_session.commit()

        doc_id = client.post(
            f"/ranges/{rng.id}/documents",
            files=[("files", ("a.txt", io.BytesIO(b"hello"), "text/plain"))],
        ).json()[0]["id"]

        assert client.get(f"/ranges/{other.id}/documents/{doc_id}").status_code == 404
        assert client.delete(f"/ranges/{other.id}/documents/{doc_id}").status_code == 404
