# Vendored schemas for ARC²

`CourseStructure.xsd` (cmi5 Quartz) belongs here, next to its Apache-2.0 `LICENSE`. It is not
checked in yet, so `arc2.check` reports `cmi5.xml_xsd` as a **human** finding
(`xsd_not_vendored`) and blocks promotion, never a pass.

Fetching it is a network action, so it is done by a person on a box that may reach GitHub, not
by an agent (content-pack rule: no external calls from agents):

```bash
git clone --depth 1 --branch quartz https://github.com/AICC/CMI-5_Spec_Current.git /tmp/cmi5-spec
cp /tmp/cmi5-spec/v1/CourseStructure.xsd tools/arc2/vendor/CourseStructure.xsd
cp /tmp/cmi5-spec/LICENSE tools/arc2/vendor/LICENSE-cmi5
```

Then check whether the XSD imports `http://www.w3.org/2001/xml.xsd`; if it does, vendor that
file too and point the `schemaLocation` at it. `arc2.cmi5` parses with `no_network=True`, so a
missing import fails loudly instead of fetching.

Source of the reference: `docs/xAPI CMI5 Reference/xapi-cmi5-kb/` (§9.14, `scripts/fetch-sources.sh`).
