# cmi5 course structure schema (vendored)

`CourseStructure.xsd` is the cmi5 (Quartz) course structure schema, namespace
`https://w3id.org/xapi/profiles/cmi5/v1/CourseStructure.xsd`, copied unchanged from the
AICC spec repository under its Apache-2.0 licence (`LICENSE`, same repository):

| | |
|---|---|
| Source | `https://github.com/AICC/CMI-5_Spec_Current`, branch `quartz`, `v1/CourseStructure.xsd` |
| Last change upstream | commit `5b99e600bc3c904da0b1634b4e9d8b1891f2e7ab` (2016-02-05) |
| SHA-256 | `6a0b04962f5baa4603ee28e84ce4c74a122f322a548c8fdc14d93b2a23781d75` |
| Fetched | 2026-10-09 |

It imports nothing, so validation needs no network. `tests/api/test_cmi5_structure.py`
validates every `cmi5.xml` TrueNorth serves (`GET /cmi5/releases/{id}/cmi5.xml`) against it.
ARC²'s own check (`tools/arc2`, `XSD_PATH`) looks for a copy in `tools/arc2/vendor/`; it
can point here instead.
