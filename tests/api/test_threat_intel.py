"""Threat intelligence feeds: CRUD, pulling a feed into indicators, and the ways a pull fails.

Before this, a feed could be recorded but nothing ever fetched it or created an indicator.
A pull goes through the feed's backend (app/threat_intel_backends, ADR 0001) and is stored
as the feed's indicators inside the feed's tenant (app/threat_intel_sync).
"""

from __future__ import annotations

import json
import socket
import uuid

import httpx
import pytest
import respx
from _shared import DEV_TENANT, OTHER_TENANT, acting_as
from app.models import AuditLog, ThreatIndicator, ThreatIntelFeed, UserRole
from app.threat_intel_backends import csv_feed

FEED_URL = "https://feeds.example.org/iocs.csv"
PUBLIC_IP = "93.184.216.34"

CSV = b"""type,value,first_seen,mitre_technique,severity,confidence,name
ipv4,203.0.113.7,2026-10-01,T1071.001,high,80,c2 server
domain,Northwind-Update.example,2026-10-01T08:00:00Z,T1071;T1568.002,critical,90,c2 domain
sha256,E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855,,T1204.002,,,dropper
"""


def _resolve_to(ip: str):
    def fake(host, port, type=0, **_):  # noqa: A002 - socket.getaddrinfo's own name
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(csv_feed, "_resolve", _resolve_to(PUBLIC_IP))


def _feed(client, **extra) -> dict:
    body = {"name": f"feed-{uuid.uuid4().hex[:6]}", "feed_type": "csv", "url": FEED_URL, **extra}
    resp = client.post("/threat-intel/feeds", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload(client, feed_id, content: bytes, name="feed.csv"):
    return client.post(f"/threat-intel/feeds/{feed_id}/upload", files={"file": (name, content, "text/csv")})


def _indicators(db, feed_id) -> dict[tuple[str, str], ThreatIndicator]:
    db.expire_all()
    rows = db.query(ThreatIndicator).filter(ThreatIndicator.feed_id == uuid.UUID(str(feed_id))).all()
    return {(r.indicator_type, r.value): r for r in rows}


def _foreign_feed(db) -> ThreatIntelFeed:
    feed = ThreatIntelFeed(name="theirs", feed_type="csv", url=FEED_URL, tenant_id=uuid.UUID(OTHER_TENANT))
    db.add(feed)
    db.commit()
    return feed


# -- CRUD ----------------------------------------------------------------------------------
class TestFeedCrud:
    def test_create_read_update_delete(self, client):
        feed = _feed(client)
        assert feed["feed_type"] == "csv" and feed["indicator_count"] == 0 and feed["last_poll_status"] is None
        assert client.get(f"/threat-intel/feeds/{feed['id']}").json()["name"] == feed["name"]
        assert feed["id"] in {f["id"] for f in client.get("/threat-intel/feeds").json()}

        resp = client.patch(f"/threat-intel/feeds/{feed['id']}", json={"poll_interval_minutes": 15})
        assert resp.status_code == 200 and resp.json()["poll_interval_minutes"] == 15

        assert client.delete(f"/threat-intel/feeds/{feed['id']}").status_code == 204
        assert client.get(f"/threat-intel/feeds/{feed['id']}").status_code == 404
        assert feed["id"] not in {f["id"] for f in client.get("/threat-intel/feeds").json()}

    @pytest.mark.parametrize("bad", [{"feed_type": "rss"}, {"name": ""}, {"poll_interval_minutes": 1}])
    def test_invalid_feeds_are_refused(self, client, bad):
        body = {"name": "x", "feed_type": "csv", **bad}
        assert client.post("/threat-intel/feeds", json=body).status_code == 422

    def test_a_missing_feed_is_404_for_every_verb(self, client):
        missing = uuid.uuid4()
        assert client.get(f"/threat-intel/feeds/{missing}").status_code == 404
        assert client.patch(f"/threat-intel/feeds/{missing}", json={"name": "y"}).status_code == 404
        assert client.delete(f"/threat-intel/feeds/{missing}").status_code == 404
        assert client.post(f"/threat-intel/feeds/{missing}/pull").status_code == 404
        assert _upload(client, missing, CSV).status_code == 404

    def test_a_student_cannot_create_or_pull_feeds(self, client):
        feed = _feed(client)
        with acting_as(UserRole.student):
            assert client.post("/threat-intel/feeds", json={"name": "s", "feed_type": "csv"}).status_code == 403
            assert client.post(f"/threat-intel/feeds/{feed['id']}/pull").status_code == 403
            assert _upload(client, feed["id"], CSV).status_code == 403


# -- pulling -------------------------------------------------------------------------------
class TestPull:
    @respx.mock
    def test_a_pull_from_the_url_creates_the_feeds_indicators(self, client, db_session, public_dns):
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=CSV))
        feed = _feed(client)

        resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["status"], body["created"], body["updated"], body["rejected"]) == ("ok", 3, 0, 0)
        assert body["feed"]["indicator_count"] == 3 and body["feed"]["last_poll_status"] == "ok"
        rows = _indicators(db_session, feed["id"])
        ip = rows[("ipv4", "203.0.113.7")]
        assert (ip.severity, ip.confidence, ip.name) == ("high", 80, "c2 server")
        assert json.loads(ip.mitre_attack_ids) == ["T1071.001"] and ip.valid_from.year == 2026
        dom = rows[("domain", "northwind-update.example")]  # normalised
        assert json.loads(dom.mitre_attack_ids) == ["T1071", "T1568.002"]
        sha = rows[("sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")]
        assert (sha.severity, sha.confidence, sha.mitre_attack_ids) == ("medium", 50, '["T1204.002"]')
        assert all(str(r.tenant_id) == DEV_TENANT and r.is_active for r in rows.values())
        audit = db_session.query(AuditLog).filter(AuditLog.action == "threat_intel.feed.pulled").one()
        assert audit.resource_id == feed["id"] and "3 created" in audit.detail

    def test_an_uploaded_csv_is_read_as_the_feed(self, client, db_session):
        feed = _feed(client, url=None)  # an upload needs no URL
        resp = _upload(client, feed["id"], CSV)
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] == 3
        listed = client.get("/threat-intel/indicators", params={"feed_id": feed["id"]}).json()
        assert {i["value"] for i in listed} >= {"203.0.113.7", "northwind-update.example"}

    def test_a_second_pull_updates_and_deactivates_what_the_feed_dropped(self, client, db_session):
        feed = _feed(client, url=None)
        _upload(client, feed["id"], CSV)
        second = b"type,value,severity\nipv4,203.0.113.7,low\nurl,http://evil.example/payload,high\n"

        body = _upload(client, feed["id"], second).json()

        assert (body["created"], body["updated"], body["deactivated"]) == (1, 1, 2)
        assert body["feed"]["indicator_count"] == 2
        rows = _indicators(db_session, feed["id"])
        assert rows[("ipv4", "203.0.113.7")].severity == "low"
        assert rows[("domain", "northwind-update.example")].is_active is False  # kept, not deleted
        assert len(rows) == 4
        active = client.get("/threat-intel/indicators", params={"feed_id": feed["id"]}).json()
        assert {i["value"] for i in active} == {"203.0.113.7", "http://evil.example/payload"}

    def test_a_feed_that_names_a_value_twice_keeps_one_row_the_later(self, client, db_session):
        feed = _feed(client, url=None)
        body = _upload(client, feed["id"], b"type,value,severity\nipv4,198.51.100.1,low\nip,198.51.100.1,high\n").json()
        assert body["created"] == 1
        assert _indicators(db_session, feed["id"])[("ipv4", "198.51.100.1")].severity == "high"

    def test_a_disabled_feed_is_not_pulled(self, client):
        feed = _feed(client, is_enabled=False)
        assert client.post(f"/threat-intel/feeds/{feed['id']}/pull").status_code == 409
        assert _upload(client, feed["id"], CSV).status_code == 409

    @pytest.mark.parametrize("feed_type", ["taxii", "stix_file", "custom_api"])
    def test_a_feed_type_with_no_backend_says_which_can_be_pulled(self, client, feed_type):
        feed = _feed(client, feed_type=feed_type)
        resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")
        assert resp.status_code == 422 and "csv" in resp.json()["detail"]

    def test_an_oversize_upload_is_413(self, client, monkeypatch):
        monkeypatch.setenv("THREAT_INTEL_MAX_BYTES", "64")
        feed = _feed(client, url=None)
        assert _upload(client, feed["id"], CSV).status_code == 413


# -- malformed feeds and rows --------------------------------------------------------------
class TestMalformed:
    @pytest.mark.parametrize(
        ("content", "says"),
        [
            (b"", "empty"),
            (b"   \n\n", "empty"),
            (b"indicator,kind\n1.2.3.4,ip\n", "no type, value column"),
            (b"type\nipv4\n", "no value column"),
            (b"\xff\xfe\x00b\x00a\x00d", "not UTF-8"),
            (b"type,value\nipv4,\x00\n", "NUL"),
        ],
    )
    def test_a_feed_that_is_not_a_feed_is_refused_whole_and_recorded(self, client, db_session, content, says):
        feed = _feed(client, url=None)
        resp = _upload(client, feed["id"], content)
        assert resp.status_code == 422 and says in resp.json()["detail"], resp.text
        assert _indicators(db_session, feed["id"]) == {}
        stored = client.get(f"/threat-intel/feeds/{feed['id']}").json()
        assert stored["last_poll_status"] == "error: malformed" and stored["last_poll_at"]

    def test_bad_rows_are_rejected_by_row_number_and_the_rest_is_kept(self, client, db_session):
        content = (
            b"type,value,first_seen,mitre_technique,severity,confidence\n"
            b"ipv4,203.0.113.9,,,,\n"                     # 2 ok
            b"ipv4,999.1.1.1,,,,\n"                       # 3 not an address
            b"ipv6,203.0.113.10,,,,\n"                    # 4 v4 given as v6
            b"domain,-bad-.example,,,,\n"                 # 5
            b"md5,abc,,,,\n"                              # 6 too short
            b"sha256,203.0.113.11,,,,\n"                  # 7
            b"url,javascript:alert(1),,,,\n"              # 8
            b"email,not-an-email,,,,\n"                   # 9
            b"registry_key,HKLM\\Run,,,,\n"               # 10 unknown type
            b"domain,ok.example,yesterday,,,\n"           # 11 bad date
            b"domain,ok2.example,,T59,,\n"                # 12 unknown MITRE id
            b"domain,ok3.example,,attack.t1059,,\n"       # 13 unknown MITRE id
            b"domain,ok4.example,,,urgent,\n"             # 14 bad severity
            b"domain,ok5.example,,,,101\n"                # 15 bad confidence
            b"domain,ok6.example,,,,high\n"               # 16 bad confidence
            b",203.0.113.12,,,,\n"                        # 17 no type
            b"domain,ok7.example,,,,,extra\n"             # 18 too many cells
            b"email,Analyst@Example.ORG,,TA0001,,\n"      # 19 ok
        )
        feed = _feed(client, url=None)

        body = _upload(client, feed["id"], content).json()

        assert body["status"] == "partial" and body["created"] == 2 and body["rejected"] == 16
        by_row = {r["row"]: r["reason"] for r in body["rejections"]}
        assert set(by_row) == set(range(3, 19))
        assert "ipv4" in by_row[3] and "ipv6" in by_row[4] and "unknown indicator type" in by_row[10]
        assert "MITRE" in by_row[12] and "MITRE" in by_row[13] and "first_seen" in by_row[11]
        assert "999.1.1.1" not in json.dumps(body["rejections"])  # reasons never echo content
        assert set(_indicators(db_session, feed["id"])) == {("ipv4", "203.0.113.9"), ("email", "analyst@example.org")}
        assert client.get(f"/threat-intel/feeds/{feed['id']}").json()["last_poll_status"] == "partial"

    def test_only_the_first_fifty_rejections_are_listed(self, client):
        content = b"type,value\n" + b"ipv4,nope\n" * 120
        body = _upload(client, _feed(client, url=None)["id"], content).json()
        assert body["rejected"] == 120 and len(body["rejections"]) == 50


# -- unreachable and refused URLs ----------------------------------------------------------
class TestUnreachable:
    @respx.mock
    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(404),
            httpx.Response(503),
            httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
        ],
        ids=["404", "503", "redirect", "refused", "timeout"],
    )
    def test_a_feed_that_cannot_be_fetched_is_502_and_recorded(self, client, db_session, public_dns, answer):
        route = respx.get(FEED_URL)
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        feed = _feed(client)

        resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")

        assert resp.status_code == 502, resp.text
        stored = client.get(f"/threat-intel/feeds/{feed['id']}").json()
        assert stored["last_poll_status"] == "error: unreachable" and stored["indicator_count"] == 0
        assert _indicators(db_session, feed["id"]) == {}

    def test_a_host_that_does_not_resolve_is_502(self, client, monkeypatch):
        def no_dns(*_a, **_k):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(csv_feed, "_resolve", no_dns)
        feed = _feed(client)
        assert client.post(f"/threat-intel/feeds/{feed['id']}/pull").status_code == 502

    def test_a_previous_good_pull_survives_a_failed_one(self, client, db_session, public_dns):
        feed = _feed(client)
        _upload(client, feed["id"], CSV)
        with respx.mock:
            respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("down"))
            assert client.post(f"/threat-intel/feeds/{feed['id']}/pull").status_code == 502
        rows = _indicators(db_session, feed["id"])
        assert len(rows) == 3 and all(r.is_active for r in rows.values())

    def test_a_feed_without_a_url_must_be_uploaded(self, client):
        feed = _feed(client, url=None)
        resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")
        assert resp.status_code == 422 and "upload" in resp.json()["detail"]

    @pytest.mark.parametrize(
        ("url", "ip"),
        [
            ("http://localhost:9200/_search", "127.0.0.1"),
            ("http://metadata.internal/latest", "169.254.169.254"),
            ("http://opensearch:9200/", "172.18.0.5"),
            ("http://[::1]/feed.csv", "::1"),
            ("file:///etc/passwd", PUBLIC_IP),
            ("ftp://feeds.example.org/iocs.csv", PUBLIC_IP),
        ],
    )
    def test_the_api_does_not_fetch_internal_or_non_http_urls(self, client, db_session, monkeypatch, url, ip):
        monkeypatch.setattr(csv_feed, "_resolve", _resolve_to(ip))
        feed = _feed(client, url=url)
        with respx.mock:  # nothing may be requested; respx fails any unmocked call
            resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")
        assert resp.status_code == 422, resp.text
        assert client.get(f"/threat-intel/feeds/{feed['id']}").json()["last_poll_status"] == "error: refused"

    @respx.mock
    def test_a_private_feed_host_is_allowed_when_the_range_says_so(self, client, monkeypatch):
        monkeypatch.setenv("THREAT_INTEL_ALLOW_PRIVATE_FEEDS", "true")
        monkeypatch.setattr(csv_feed, "_resolve", _resolve_to("10.20.0.5"))
        respx.get("http://intel.range.local/iocs.csv").mock(return_value=httpx.Response(200, content=CSV))
        feed = _feed(client, url="http://intel.range.local/iocs.csv")
        assert client.post(f"/threat-intel/feeds/{feed['id']}/pull").json()["created"] == 3

    @respx.mock
    def test_an_oversize_fetched_feed_is_refused(self, client, monkeypatch, public_dns):
        monkeypatch.setenv("THREAT_INTEL_MAX_BYTES", "64")
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=CSV))
        feed = _feed(client)
        resp = client.post(f"/threat-intel/feeds/{feed['id']}/pull")
        assert resp.status_code == 422 and "larger than 64 bytes" in resp.json()["detail"]


# -- tenant isolation ----------------------------------------------------------------------
class TestTenantIsolation:
    def test_another_tenants_feed_cannot_be_pulled_uploaded_or_read(self, client, db_session):
        theirs = _foreign_feed(db_session)
        assert client.post(f"/threat-intel/feeds/{theirs.id}/pull").status_code == 404
        assert _upload(client, theirs.id, CSV).status_code == 404
        assert client.get(f"/threat-intel/feeds/{theirs.id}").status_code == 404
        assert client.get(f"/threat-intel/feeds/{theirs.id}/indicators").status_code == 404
        assert str(theirs.id) not in {f["id"] for f in client.get("/threat-intel/feeds").json()}
        assert _indicators(db_session, theirs.id) == {}

    def test_the_same_value_in_two_tenants_stays_two_rows_and_each_sees_its_own(self, client, db_session):
        theirs = _foreign_feed(db_session)
        foreign = ThreatIndicator(feed_id=theirs.id, tenant_id=theirs.tenant_id, indicator_type="ipv4",
                                  value="203.0.113.7", severity="low", confidence=10, is_active=True)
        db_session.add(foreign)
        db_session.commit()
        mine = _feed(client, url=None)

        _upload(client, mine["id"], CSV)

        db_session.expire_all()
        assert (foreign.severity, foreign.confidence, foreign.is_active) == ("low", 10, True)  # untouched
        listed = client.get("/threat-intel/indicators", params={"value": "203.0.113.7"}).json()
        assert [i["feed_id"] for i in listed] == [mine["id"]]
        assert client.get(f"/threat-intel/indicators/{foreign.id}").status_code == 404
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            theirs_view = client.get("/threat-intel/indicators", params={"value": "203.0.113.7"}).json()
        assert [i["id"] for i in theirs_view] == [str(foreign.id)]
