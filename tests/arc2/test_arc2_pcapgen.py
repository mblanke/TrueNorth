"""Synthetic teaching captures: tools/arc2/pcapgen.py and the engine checks around it.

The point of the design is that an agent writes a reviewable spec and never packet bytes.
These tests pin the three properties that make that safe: a spec can only describe benign,
documentation-range traffic; rendering is deterministic; and the engine rejects any pcap
that is not exactly what its spec renders to.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import struct

import pytest
from arc2 import check, cmi5, pcapgen
from test_arc2_manifest import checks, full_manifest, make_run

SPEC = {
    "schema": "arc2/capture/0.1",
    "id": "inj-06-architecture",
    "seed": 42,
    "start": "2026-10-01T13:00:00Z",
    "segments": [
        {"id": "users", "cidr": "192.0.2.0/25", "gateway": "gw-users"},
        {"id": "servers", "cidr": "198.51.100.0/25", "gateway": "gw-servers"},
    ],
    "hosts": [
        {"id": "ws-01", "ip": "192.0.2.10", "name": "ws-01.corp.example", "role": "workstation"},
        {"id": "gw-users", "ip": "192.0.2.1", "role": "gateway"},
        {"id": "gw-servers", "ip": "198.51.100.1", "role": "gateway"},
        {"id": "dns-01", "ip": "198.51.100.53", "name": "dns-01.corp.example", "role": "dns"},
        {"id": "web-01", "ip": "198.51.100.80", "name": "intranet.corp.example", "role": "web"},
        {"id": "ext", "ip": "203.0.113.10", "name": "www.example", "role": "external"},
    ],
    "events": [
        {"at": 0, "type": "arp", "src": "ws-01", "target": "gw-users"},
        {"at": 0.5, "type": "dns", "src": "ws-01", "server": "dns-01", "query": "intranet.corp.example"},
        {"at": 1, "type": "http", "src": "ws-01", "dst": "web-01", "path": "/index.html", "status": 200},
        {"at": 2, "type": "ping", "src": "ws-01", "dst": "web-01", "count": 2},
        {"at": 5, "type": "tls", "src": "ws-01", "dst": "ext", "sni": "www.example"},
        {"at": 6, "type": "tcp_refused", "src": "ws-01", "dst": "web-01", "port": 22},
    ],
}


def spec(**over) -> dict:
    s = copy.deepcopy(SPEC)
    s.update(over)
    return s


def test_rendering_is_deterministic_and_reads_back():
    data1, summary = pcapgen.render(spec())
    data2, _ = pcapgen.render(spec())
    assert data1 == data2
    frames = pcapgen.read_pcap(data1)
    assert len(frames) == summary["packets"]
    assert [t for t, _ in frames] == sorted(t for t, _ in frames)


def test_every_ipv4_header_checksum_is_valid():
    data, _ = pcapgen.render(spec())
    for _, frame in pcapgen.read_pcap(data):
        if frame[12:14] == b"\x08\x00":
            ihl = (frame[14] & 0x0F) * 4
            assert pcapgen._csum(frame[14:14 + ihl]) == 0


def test_traffic_between_segments_is_seen_on_both_legs_through_the_gateways():
    data, summary = pcapgen.render(spec())
    macs = {h["id"]: bytes.fromhex(h["mac"].replace(":", "")) for h in summary["hosts"]}
    ping = [f for _, f in pcapgen.read_pcap(data) if f[12:14] == b"\x08\x00" and f[23] == 1 and f[34] == 8]
    first_two = ping[:2]  # the first echo request, once per leg
    assert first_two[0][0:6] == macs["gw-users"] and first_two[0][6:12] == macs["ws-01"]
    assert first_two[1][0:6] == macs["web-01"] and first_two[1][6:12] == macs["gw-servers"]
    assert first_two[0][22] - first_two[1][22] == 1  # TTL drops at the router


def test_the_summary_is_the_answer_key():
    _, summary = pcapgen.render(spec())
    assert {s["id"]: s["gateway_ip"] for s in summary["segments"]} == {"users": "192.0.2.1", "servers": "198.51.100.1"}
    roles = {h["id"]: (h["segment"], h["role"]) for h in summary["hosts"]}
    assert roles["ext"] == (None, "external") and roles["web-01"] == ("servers", "web")
    assert summary["dns"][0]["answer"] == "198.51.100.80"
    assert summary["tls"] == [{"client": "ws-01", "server": "ext", "sni": "www.example"}]
    assert set(summary["conversations"]) == {"arp", "dns", "http", "icmp", "tls", "tcp_refused"}


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s["hosts"][0].update(ip="10.0.0.5"), "must be in 192.0.2.0/24"),
    (lambda s: s["hosts"][0].update(ip="8.8.8.8"), "must be in 192.0.2.0/24"),
    (lambda s: s["hosts"][0].update(name="ws-01.forces.gc.ca"), "under .example"),
    (lambda s: s["events"].append({"at": 1, "type": "exploit", "src": "ws-01", "dst": "web-01"}), "type must be one of"),
    (lambda s: s["events"].append({"at": 1, "type": "http", "src": "ws-01", "dst": "web-01", "payload": "\\x90"}), "unknown field"),
    (lambda s: s["events"].append({"at": 1, "type": "ping", "src": "ws-01", "dst": "web-01", "count": 500}), "count must be"),
    (lambda s: s["segments"][0].update(cidr="10.1.0.0/16"), "documentation range"),
    (lambda s: s["hosts"].append({"id": "stray", "ip": "203.0.113.20", "role": "workstation"}), "only role external"),
])
def test_specs_cannot_describe_real_networks_or_payloads(mutate, message):
    s = spec()
    mutate(s)
    errs = pcapgen.validate(s)
    assert any(message in e for e in errs), errs
    with pytest.raises(pcapgen.SpecError):
        pcapgen.render(s)


def test_only_documentation_addresses_appear_in_the_capture():
    data, _ = pcapgen.render(spec())
    allowed = pcapgen.ALLOWED_NETS
    for _, frame in pcapgen.read_pcap(data):
        if frame[12:14] == b"\x08\x00":
            for raw in (frame[26:30], frame[30:34]):
                assert any(ipaddress.ip_address(raw) in n for n in allowed)


# ── the engine around it ────────────────────────────────────────────────


def run_with_capture(tmp_path, tamper=None, author_required=False):
    manifest = full_manifest()
    inj = manifest["injects"]["items"][0]
    inj["author_required"] = author_required
    inj["capture"] = {"spec": "03-range/captures/cap.yaml", "pcap": "03-range/captures/cap.pcap",
                      "summary": "03-range/captures/cap.summary.json"}
    run = make_run(tmp_path, manifest)
    cap = run / "03-range" / "captures"
    cap.mkdir(parents=True, exist_ok=True)
    import yaml
    (cap / "cap.yaml").write_text(yaml.safe_dump(SPEC))
    data, summary = pcapgen.render(SPEC)
    (cap / "cap.pcap").write_bytes(tamper(data) if tamper else data)
    (cap / "cap.summary.json").write_text(json.dumps(summary))
    return run, check.load_manifest(run)


def test_the_engine_accepts_a_capture_that_matches_its_spec(tmp_path):
    run, manifest = run_with_capture(tmp_path)
    found = check._check_captures(run, manifest)
    assert found == []


def test_a_capture_edited_after_rendering_is_rejected(tmp_path):
    def tamper(data):
        return data[:-4] + struct.pack("<I", 0xDEADBEEF)
    run, manifest = run_with_capture(tmp_path, tamper=tamper)
    assert checks(check._check_captures(run, manifest)) == {"inject.capture_matches_spec"}


def test_a_generated_capture_cannot_also_be_left_to_a_person(tmp_path):
    run, manifest = run_with_capture(tmp_path, author_required=True)
    assert "inject.capture_author_required" in checks(check._check_captures(run, manifest))


def test_the_package_ships_the_capture_as_a_download_but_not_its_answer_key(tmp_path):
    run, manifest = run_with_capture(tmp_path)
    block, files = cmi5.write_package(run, manifest)
    root = run / cmi5.PACKAGE_ROOT
    inj = manifest["injects"]["items"][0]
    assert (root / "resources" / f"{inj['id']}.pcap").read_bytes() == (run / "03-range/captures/cap.pcap").read_bytes()
    assert not list(root.rglob("*.summary.json"))
    au = next(a for a in block["aus"] if inj["objective_id"] in a["objective_ids"])
    page = (root / au["module_id"] / "index.html").read_text()
    assert f'href="../resources/{inj["id"]}.pcap"' in page
    assert any(f["path"].endswith(f"resources/{inj['id']}.pcap") for f in files)


def test_capture_specs_and_summaries_pass_the_defang_scan(tmp_path):
    from arc2 import qa
    run, _ = run_with_capture(tmp_path)
    hits = [f for f in qa.check_defang(run) if "captures/" in (f.get("path") or f.get("message", ""))]
    assert hits == [], hits
