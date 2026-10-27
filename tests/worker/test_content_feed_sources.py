"""The content feed sources (worker/feed_backends; ADR 0014 decision 25, issue #287).

Each source reads recorded payloads (tests/worker/content_feeds_fixtures) through a fake
``FeedHttp``: no network. Also: ``FeedHttp`` itself (GET only, the configured proxy and no
other, errors that carry no query or key) and the product map.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from worker.content_feeds import catalogue
from worker.feed_backends import FeedError, FeedHttp, FeedProduct, FeedUnreachableError, get_feed_source
from worker.feed_backends import http as http_mod
from worker.feed_backends.cisa_kev import URL as KEV_URL
from worker.feed_backends.cisa_kev import CisaKevSource
from worker.feed_backends.endoflife import EndOfLifeSource
from worker.feed_backends.nvd import SPACING_PUBLIC, SPACING_WITH_KEY, NvdSource

FIX = Path(__file__).parent / "content_feeds_fixtures"
NOW = datetime(2026, 10, 27, 4, 10, tzinfo=UTC)
UBUNTU = FeedProduct(name="Ubuntu 24.04", eol_product="ubuntu", eol_cycle="24.04", release="24.04.5",
                     cpe="cpe:2.3:o:canonical:ubuntu_linux:24.04")
FOCAL = FeedProduct(name="Ubuntu 20.04", eol_product="ubuntu", eol_cycle="20.04")
SRV2022 = FeedProduct(name="Windows Server 2022", eol_product="windows-server", eol_cycle="2022",
                      cpe="cpe:2.3:o:microsoft:windows_server_2022", kev=(("Microsoft", "Windows"),))
EXCHANGE = FeedProduct(name="Exchange Server 2019", kev=(("Microsoft", "Exchange Server"),))


def fixture(name: str):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    """Answers GETs from a route table: a payload, a callable(params) or an exception."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def get_json(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer


# ── endoflife.date ───────────────────────────────────────────────────────────
class TestEndOfLife:
    def routes(self):
        return {"https://endoflife.date/api/ubuntu.json": fixture("endoflife_ubuntu.json"),
                "https://endoflife.date/api/windows-server.json": fixture("endoflife_windows_server.json")}

    def test_end_of_life_and_latest_release_of_each_mapped_cycle(self):
        http = FakeHttp(self.routes())
        out = EndOfLifeSource().fetch(http, [UBUNTU, FOCAL, SRV2022], since=None, now=NOW)
        got = {e.key: e for e in out.entries}
        assert set(got) == {"eol:ubuntu:24.04", "release:ubuntu:24.04:24.04.6", "eol:ubuntu:20.04",
                            "release:ubuntu:20.04:20.04.6", "eol:windows-server:2022",
                            "release:windows-server:2022:10.0.20348"}
        focal = got["eol:ubuntu:20.04"]
        assert focal.kind == "release" and focal.at == datetime(2025, 5, 31, tzinfo=UTC)
        assert focal.products == ("eol:ubuntu:20.04",) and focal.data["eol"] == "2025-05-31"
        assert got["release:ubuntu:24.04:24.04.6"].data["latest"] == "24.04.6"
        assert out.errors == []
        # one GET per product, never a cycle the map does not name (22.04, 24.10)
        assert sorted(c[0] for c in http.calls) == sorted(self.routes())

    def test_a_cycle_whose_end_of_life_is_true_without_a_date_is_dated_now(self):
        old = FeedProduct(name="Windows Server 2012 R2", eol_product="windows-server", eol_cycle="2012-r2")
        out = EndOfLifeSource().fetch(FakeHttp(self.routes()), [old], since=None, now=NOW)
        eol = next(e for e in out.entries if e.key == "eol:windows-server:2012-r2")
        assert eol.at == NOW and eol.data["eol"] is None

    def test_a_product_the_feed_does_not_know_is_a_per_product_error(self):
        routes = {**self.routes(), "https://endoflife.date/api/nosuch.json": FeedUnreachableError("endoflife.date "
                                                                                                    "answered HTTP 404")}
        bogus = FeedProduct(name="Nosuch 1", eol_product="nosuch", eol_cycle="1")
        out = EndOfLifeSource().fetch(FakeHttp(routes), [UBUNTU, bogus], since=None, now=NOW)
        assert out.entries and out.errors == ["nosuch: endoflife.date answered HTTP 404"]

    def test_when_every_product_fails_the_feed_failed(self):
        routes = {"https://endoflife.date/api/ubuntu.json": FeedUnreachableError("endoflife.date could not be "
                                                                                 "reached (ConnectError)")}
        with pytest.raises(FeedError, match="could not be reached"):
            EndOfLifeSource().fetch(FakeHttp(routes), [UBUNTU], since=None, now=NOW)


# ── CISA KEV ─────────────────────────────────────────────────────────────────
class TestCisaKev:
    def test_only_vulnerabilities_of_mapped_products_are_kept(self):
        out = CisaKevSource().fetch(FakeHttp({KEV_URL: fixture("cisa_kev.json")}), [SRV2022, EXCHANGE],
                                    since=None, now=NOW)
        got = {e.key: e for e in out.entries}
        assert set(got) == {"kev:CVE-2026-21001", "kev:CVE-2026-20777", "kev:CVE-2019-0708"}  # not Accellion
        ex = got["kev:CVE-2026-21001"]
        assert ex.kind == "vulnerability" and ex.products == ("kev:microsoft:exchange server",)
        assert ex.at == datetime(2026, 10, 23, tzinfo=UTC)
        assert ex.title == "Microsoft Exchange Server Remote Code Execution Vulnerability"

    def test_no_mapped_product_no_request(self):
        http = FakeHttp({})
        assert CisaKevSource().fetch(http, [FOCAL], since=None, now=NOW).entries == []
        assert http.calls == []

    def test_a_catalogue_without_vulnerabilities_is_malformed(self):
        with pytest.raises(FeedError):
            CisaKevSource().fetch(FakeHttp({KEV_URL: {"title": "x"}}), [SRV2022], since=None, now=NOW)


# ── NVD ──────────────────────────────────────────────────────────────────────
class TestNvd:
    URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"

    def test_cves_at_or_above_the_threshold_for_each_product_cpe(self):
        http = FakeHttp({self.URL: fixture("nvd_windows_server_2022.json")})
        sleeps: list[float] = []
        src = NvdSource(min_cvss=9.0, sleep=sleeps.append)
        out = src.fetch(http, [SRV2022], since=None, now=NOW)
        assert [e.key for e in out.entries] == ["nvd:CVE-2026-30100"]  # 5.5 and unscored are not kept
        e = out.entries[0]
        assert e.products == ("cpe:cpe:2.3:o:microsoft:windows_server_2022",)
        assert e.data == {"cve": "CVE-2026-30100", "cvss": 9.8, "severity": "CRITICAL"}
        assert e.at == datetime(2026, 10, 21, 17, 15, 52, 360000, tzinfo=UTC)
        assert e.title == "Windows TCP/IP Remote Code Execution Vulnerability"
        _, params, headers = http.calls[0]
        assert params["virtualMatchString"] == "cpe:2.3:o:microsoft:windows_server_2022"
        # windowed on last modification (NVD's analysis adds CPEs and scores after publishing)
        assert params["lastModStartDate"] == (NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000")
        assert params["lastModEndDate"] == NOW.strftime("%Y-%m-%dT%H:%M:%S.000")
        assert "pubStartDate" not in params and "noRejected" not in params
        assert headers is None  # no key: no apiKey header
        assert out.cursor == NOW.isoformat()
        assert sleeps == []  # one request: nothing to space

    def test_the_window_starts_at_the_cursor_and_is_at_most_120_days(self):
        src = NvdSource(sleep=lambda s: None)
        since = NOW - timedelta(days=1)
        assert src.window(since, NOW) == (since, NOW)
        assert src.window(NOW - timedelta(days=400), NOW) == (NOW - timedelta(days=120), NOW)

    def test_requests_are_spaced_for_the_public_rate_limit_and_the_key_is_a_header(self):
        http = FakeHttp({self.URL: {"totalResults": 0, "vulnerabilities": []}})
        sleeps: list[float] = []
        NvdSource(sleep=sleeps.append).fetch(http, [UBUNTU, SRV2022], since=None, now=NOW)
        assert sleeps == [SPACING_PUBLIC]
        sleeps.clear()
        keyed = NvdSource(api_key="k-123", sleep=sleeps.append)
        keyed.fetch(http, [UBUNTU, SRV2022], since=None, now=NOW)
        assert sleeps == [SPACING_WITH_KEY]
        assert http.calls[-1][2] == {"apiKey": "k-123"}
        assert "k-123" not in repr(keyed)

    def test_pages_are_followed(self):
        page = fixture("nvd_windows_server_2022.json")

        def answer(params):
            if params["startIndex"] == 0:
                return {**page, "totalResults": 4}
            first = page["vulnerabilities"][0]
            return {"totalResults": 4, "vulnerabilities": [{"cve": {**first["cve"], "id": "CVE-2026-30200"}}]}

        http = FakeHttp({self.URL: answer})
        out = NvdSource(sleep=lambda s: None).fetch(http, [SRV2022], since=None, now=NOW)
        assert {e.key for e in out.entries} == {"nvd:CVE-2026-30100", "nvd:CVE-2026-30200"}
        assert [c[1]["startIndex"] for c in http.calls] == [0, 3]

    def test_a_failed_product_keeps_the_cursor_so_the_window_is_read_again(self):
        def answer(params):
            if "ubuntu" in params["virtualMatchString"]:
                raise FeedUnreachableError("services.nvd.nist.gov answered HTTP 503")
            return fixture("nvd_windows_server_2022.json")

        out = NvdSource(sleep=lambda s: None).fetch(FakeHttp({self.URL: answer}), [UBUNTU, SRV2022],
                                                    since=None, now=NOW)
        assert [e.key for e in out.entries] == ["nvd:CVE-2026-30100"]
        assert out.cursor is None and "HTTP 503" in out.errors[0]

    def test_the_registry_reads_the_threshold_and_key_from_settings(self, monkeypatch):
        monkeypatch.setenv("TN_FEEDS_NVD_MIN_CVSS", "7.5")
        monkeypatch.setenv("TN_FEEDS_NVD_API_KEY", "secret-key")
        src = get_feed_source("nvd")
        assert src.min_cvss == 7.5 and "secret-key" not in repr(src)


# ── FeedHttp ─────────────────────────────────────────────────────────────────
class TestFeedHttp:
    def test_the_configured_proxy_is_used_and_the_environments_is_not(self, monkeypatch):
        made: list[dict] = []

        class Recorder:
            def __init__(self, **kwargs):
                made.append(kwargs)

        monkeypatch.setattr(http_mod.httpx, "Client", Recorder)
        FeedHttp(proxy="http://proxy.site.example:3128")
        FeedHttp(proxy="  ")
        assert made[0]["proxy"] == "http://proxy.site.example:3128"
        assert made[1]["proxy"] is None
        for kwargs in made:
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

    def test_get_only_and_errors_carry_no_query_or_key(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/bad":
                return httpx.Response(403, json={"message": "forbidden"})
            return httpx.Response(200, json={"ok": True})

        http = FeedHttp(client=httpx.Client(transport=httpx.MockTransport(handler)))
        assert http.get_json("https://feed.example/ok", params={"a": "1"}) == {"ok": True}
        with pytest.raises(FeedUnreachableError) as err:
            http.get_json("https://feed.example/bad", params={"virtualMatchString": "x"}, headers={"apiKey": "k-9"})
        assert str(err.value) == "feed.example answered HTTP 403"
        assert {r.method for r in seen} == {"GET"}

    def test_a_connection_failure_names_only_the_host(self):
        def handler(request):
            raise httpx.ConnectError("proxy http://user:pw@proxy:3128 refused", request=request)

        http = FeedHttp(client=httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(FeedUnreachableError) as err:
            http.get_json("https://www.cisa.gov/feed.json")
        assert str(err.value) == "www.cisa.gov could not be reached (ConnectError)"


# ── the product map ──────────────────────────────────────────────────────────
class TestCatalogue:
    def test_the_shipped_map_parses_and_every_product_names_something(self):
        mapping = catalogue.load()
        assert "ubuntu-lts" in mapping and "srv2022" in mapping
        for cid, products in mapping.items():
            assert products, cid
            for p in products:
                assert p.product_keys(), f"{cid}: {p.name} names nothing the feeds know"

    def test_the_shipped_map_names_golden_templates_that_exist(self):
        csv = (Path(__file__).resolve().parents[2] / "content/catalogue/vm_iso_catalogue.csv").read_text()
        known = {line.split(",", 1)[0] for line in csv.splitlines()[1:] if line}
        assert set(catalogue.load()) <= known

    def test_a_malformed_map_is_refused(self):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.parse({"golden_templates": {"x": [{"eol": {"product": "u"}}]}})
        with pytest.raises(catalogue.CatalogueError):
            catalogue.parse({"nope": {}})
