# MITRE ATT&CK Enterprise catalogue

`enterprise-attack-techniques.json` is a compact, vendored copy of the MITRE ATT&CK
Enterprise matrix: every technique and sub-technique id with its name, its tactics (as
TA ids) and whether MITRE has deprecated or revoked it (and, if revoked, the id that
replaced it), plus the tactics. `attack_modified` is the newest modification date in the
source bundle, which carries no release number of its own.

TrueNorth uses it to check ATT&CK ids:

- detection rules (`/detection-rules`): an unknown or revoked id is refused with 422;
- `content/detections/*.yaml`: `tests/contracts/test_attack_catalogue.py`;
- telemetry tagging (`telemetry_mitre.py`): only ids the catalogue knows are tagged.

Code reads it through `control-plane/{api/app,worker/worker}/attack_catalogue.py`: from
`TN_ATTACK_CATALOGUE`, else `/app/content/mitre` (mounted read-only into the `api` and
`worker-telemetry` containers by the compose files), else this checkout.

## Updating

Generated, never hand-edited:

    python scripts/update_attack_catalogue.py                      # MITRE CTI, master
    python scripts/update_attack_catalogue.py --source bundle.json   # an offline copy

The source is `enterprise-attack/enterprise-attack.json` in MITRE's public CTI repository
(github.com/mitre/cti). On an air-gapped host, carry that file in and use `--source`.
After an update, run the tests: a technique MITRE has since revoked fails the content
test until the detection that uses it moves to the replacement id.

## Terms of use and attribution

This file is derived from MITRE ATT&CK(R).

> (c) The MITRE Corporation. This work is reproduced and distributed with the
> permission of The MITRE Corporation.

MITRE ATT&CK and ATT&CK are registered trademarks of The MITRE Corporation. Use of the
data is subject to the ATT&CK terms of use:
<https://attack.mitre.org/resources/legal-and-branding/terms-of-use/>. The same
attribution and the copyright statement from the source bundle are carried inside the
JSON file (`attribution`, `copyright`), so it travels with the data.
