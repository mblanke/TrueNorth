#!/usr/bin/env python3
"""Fill the depot's chocolatey-hosted repository with every catalogue package and its dependencies.

Runs on TN-BUILD01 (stdlib only). For each Chocolatey id in the software catalogue:
  1. resolve the version (pinned in the catalogue, else upstream's latest stable),
  2. download the .nupkg through the depot's chocolatey-proxy (this also warms the proxy;
     falls back to community.chocolatey.org if the proxy cannot fetch it),
  3. upload it to chocolatey-hosted unless that exact version is already there,
  4. queue its <dependencies>,
  5. flag install scripts that download from the internet at install time.

Why hosted and not just the proxy: offline, a NuGet v2 query that the proxy never saw
cannot be answered, whereas the hosted repository answers every query from its own
metadata. The "chocolatey" group lists hosted first.

Step 5 matters: many community packages are wrappers whose chocolateyInstall.ps1 fetches
the vendor installer at install time (e.g. from dl.google.com). Those fail in a no-egress
range even though the .nupkg is in the depot. They need internalizing (Chocolatey for
Business, or by hand: put the installer in the raw "installers" repo and repack) or
baking into the template. The report lists them.

Credentials: NEXUS_USER / NEXUS_PASSWORD in the environment (the upload-only tn-prefetch
user). Never printed.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

ATOM = "{http://www.w3.org/2005/Atom}"
D = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
M = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
URL_RE = re.compile(r"https?://[^\s'\"`)]+", re.I)
TIMEOUT = 120


def http(url, data=None, headers=None, method=None, auth=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - fixed http(s) URLs
        return resp.status, resp.read()


def latest_version(upstream, pkg_id):
    flt = urllib.parse.quote(f"tolower(Id) eq '{pkg_id.lower()}' and IsLatestVersion")
    _, body = http(f"{upstream}Packages()?$filter={flt}")
    versions = [e.findtext(f"{M}properties/{D}Version") for e in ET.fromstring(body).iter(f"{ATOM}entry")]
    versions = [v for v in versions if v]
    if not versions:
        raise LookupError(f"{pkg_id}: not found upstream")
    return versions[0]


def download(nexus, upstream, pkg_id, version):
    try:
        _, data = http(f"{nexus}/repository/chocolatey-proxy/{pkg_id}/{version}")
        return data, "depot-proxy"
    except (urllib.error.URLError, OSError):
        _, data = http(f"{upstream}package/{pkg_id}/{version}")
        return data, "upstream"


def inspect(nupkg):
    """(id, version, [(dep_id, exact_version|None)], [urls in install scripts], embedded installers)."""
    z = zipfile.ZipFile(io.BytesIO(nupkg))
    spec_name = next(n for n in z.namelist() if n.endswith(".nuspec") and "/" not in n)
    root = ET.fromstring(z.read(spec_name))
    meta = next(el for el in root if el.tag.endswith("metadata"))
    pid = next(el.text for el in meta if el.tag.endswith("}id") or el.tag == "id")
    ver = next(el.text for el in meta if el.tag.endswith("}version") or el.tag == "version")
    deps = []
    for el in root.iter():
        if el.tag.endswith("dependency") and el.get("id"):
            rng = (el.get("version") or "").strip()
            exact = rng[1:-1] if rng.startswith("[") and rng.endswith("]") and "," not in rng else None
            deps.append((el.get("id"), exact))
    urls, embedded = set(), []
    for name in z.namelist():
        low = name.lower()
        if low.endswith((".exe", ".msi", ".msu", ".zip", ".7z")) and low.startswith("tools/"):
            embedded.append(name)
        if low.endswith(".ps1"):
            for line in z.read(name).decode("utf-8", "replace").splitlines():
                if line.lstrip().startswith("#"):
                    continue
                urls.update(u.rstrip(".,;") for u in URL_RE.findall(line))
    return pid, ver, deps, sorted(urls), embedded


def in_hosted(nexus, auth, pkg_id, version):
    q = urllib.parse.urlencode({"repository": "chocolatey-hosted", "name": pkg_id, "version": version})
    _, body = http(f"{nexus}/service/rest/v1/search?{q}", auth=auth)
    return bool(json.loads(body).get("items"))


def upload(nexus, auth, filename, nupkg):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"nuget.asset\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + nupkg + f"\r\n--{boundary}--\r\n".encode()
    http(f"{nexus}/service/rest/v1/components?repository=chocolatey-hosted", data=body, method="POST", auth=auth,
         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nexus", required=True, help="e.g. http://10.30.32.10:8081")
    ap.add_argument("--upstream", default="https://community.chocolatey.org/api/v2/")
    ap.add_argument("--packages", required=True, help="JSON list of {choco, version?}")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()
    nexus = args.nexus.rstrip("/")
    upstream = args.upstream if args.upstream.endswith("/") else args.upstream + "/"
    auth = (os.environ.get("NEXUS_USER", ""), os.environ.get("NEXUS_PASSWORD", ""))
    if not all(auth):
        sys.exit("NEXUS_USER / NEXUS_PASSWORD are not set")
    try:
        http(f"{nexus}/service/rest/v1/status")
    except (urllib.error.URLError, OSError) as exc:
        print(f"FATAL depot unreachable at {nexus}: {exc}")
        return 2

    with open(args.packages) as fh:
        queue = [(p["choco"], str(p.get("version") or "") or None, True) for p in json.load(fh)]
    seen, results = set(), []
    while queue:
        pkg_id, version, top = queue.pop(0)
        key = (pkg_id.lower(), version)
        if key in seen:
            continue
        seen.add(key)
        rec = {"id": pkg_id, "requested_version": version, "catalogue": top}
        try:
            version = version or latest_version(upstream, pkg_id)
            data, rec["source"] = download(nexus, upstream, pkg_id, version)
            pid, ver, deps, urls, embedded = inspect(data)
            rec.update(id=pid, version=ver, dependencies=[d for d, _ in deps], install_urls=urls,
                       embedded_installers=embedded, offline_safe=not urls)
            if in_hosted(nexus, auth, pid, ver):
                rec["hosted"] = "present"
            else:
                upload(nexus, auth, f"{pid}.{ver}.nupkg", data)
                rec["hosted"] = "uploaded"
            queue.extend((d, exact, False) for d, exact in deps)
        except Exception as exc:  # noqa: BLE001 - one package must not stop the rest
            rec["hosted"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        results.append(rec)
        flag = "" if rec.get("offline_safe", True) else "  NEEDS-INTERNALIZING"
        print(f"{rec['hosted']:9} {rec['id']} {rec.get('version', '?')}{flag}{'  ' + rec['error'] if 'error' in rec else ''}")

    with open(args.report, "w") as fh:
        json.dump(results, fh, indent=2)
    count = {k: sum(r["hosted"] == k for r in results) for k in ("uploaded", "present", "failed")}
    risky = sorted({r["id"] for r in results if r.get("offline_safe") is False})
    print(f"TNPREFETCH uploaded={count['uploaded']} present={count['present']} failed={count['failed']} "
          f"needs_internalizing={','.join(risky) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
