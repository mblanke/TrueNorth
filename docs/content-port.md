# Port content

Owner decision, 2026-10-27 (v1.2.0 GA): courses, range templates, scenarios and exercise templates move from the
test site (the work site) to a new site **only once they are proven**. Code:
`control-plane/api/app/content_port/`; CLI: `scripts/content_port_inprocess.py`; UI: the
Content view (`/calendar/content`), the **Proven** chip and the **Port content** panel.

Golden templates are not ported this way. They have their own procedure (OVF export plus
build record, then S2 promotion on the new site; `infra/vsphere/packer/README.md`, "Build
records and promotion"). A bundle only names the golden-template builds its content needs.

Exercise templates are ported (ADR 0017): the reusable part of a collective exercise
(objectives, MESL, teams, instructor notes, the scenario and range template it runs on).
Exercises themselves are one-offs and are not ported. Courses are individual training and a
template is never attached to one.

## Proven

Proven is computed every time it is shown. It is never stored. An item is **Proven** when
all three conditions hold. Otherwise its chip reads **Not proven: <reasons>**, and each
missing condition gives a reason.

| Condition | Holds when | Evidence shown |
|---|---|---|
| Range certified | A range built from the template **as it is now** reached *Certified for Students* (ADR 0013 §6). The record's plan hash must equal the SHA-256 of the template's current YAML, so an edited template is unproven until a range built from the new version is certified. A scenario needs its range template's certification. A course needs the certification of every range template it uses, through its bookings and its scenarios. A course with no range has nothing to certify, and the condition says so. | the certification record |
| Used with passing evidence | For an exercise template: an exercise **made from it** that completed with a passing score (a completed exercise on the same range that was not made from it proves nothing). Otherwise a **closed class** (ADR 0014 §13) with at least one Student whose result is *pass*, or a **completed exercise** whose score reached 70% of its maximum. For a template: classes whose ranges or sessions were built from it, and exercises on ranges built from it. For a scenario: classes whose sessions ran it, and exercises that ran it. For a course: its own closed classes. | the class or the exercise |
| Not stale | The content clock tracks it (ADR 0014 §22-25), and it is neither stale nor retired. | its review date |

What counts as evidence is the caller's own tenant's evidence. The platform administrator
sees every tenant's evidence. A tenant never learns about another tenant's classes or ranges.

Dependencies come from the content itself, not from the content clock's hourly edges:

- a scenario's `range_template`, given as an id or a name;
- a course's module scenarios;
- an exercise template's scenario and range template (and its scenario's range template); the node types its roles and scaling rules name must exist in that range template;
- the range templates that a course's bookings and scenarios use.

Only content in the item's own scope counts as a dependency. Another tenant's private
template is never part of a course's content. A dependency this site cannot read (missing,
deleted, or not shared with the item's tenant) leaves the item not proven: missing evidence
never passes.

Who sees it: anyone with `content_review:read`, for their tenant's content and shared
content (`GET /content-port/items/{item_type}/{item_id}/proven`).

## Exporting

Only the platform administrator who also holds `content_review:admin` can export. They use
the **Port content** panel on the Content view, or the CLI.

1. Select the courses, range templates, scenarios and exercise templates to export. Their dependencies come
   along automatically: a course pulls in its range templates and scenarios.
2. **Export proven content**. If any selected item or any dependency is not proven, the
   export is refused (409). Nothing is exported, and the refusal lists every item with its
   reasons and whether it was needed by the selection.
3. If everything is proven, you get a signed JSON bundle. The export is audit-logged
   (`content_port.export`) with the bundle id, the key and the hash.

What a bundle carries for each item:

- its content:
  - a range template: name, version and YAML;
  - a scenario: name, version, YAML, and the key of its range template;
  - an exercise template: name, description, teams, participant roles (with VM profiles), shared-infrastructure scaling rules, objectives, MESL (no delivery status), instructor
    notes, tags, and the keys of its scenario and range template (no tenant, owner, sharing or revision);
  - a course: its fields, with its qualification and performance objectives given by code,
    and its modules with their authored content, lessons, quizzes and scenario links. Of
    its `course_meta`, only the keys that describe the course are carried (an allowlist,
    `PORTABLE_META` in `bundle.py`: course code, title, programme, terms, prerequisites,
    provenance, status, locale and the like). Site state such as `test_content`, `retired`
    and `superseded_by`, and any key not on the list, never leaves, so an origin's
    test-content release cannot land as one. On import, this site's own non-portable keys
    are kept;
- its proof summary: each condition, plus the ids and dates of the evidence;
- the golden-template builds that its certified range ran (catalogue id, build id,
  checksum).

What a bundle never carries:

- Students, enrolments, progress, quiz attempts or results;
- class, range or Student names;
- credentials;
- tenant ids, owners or reviewers;
- media files;
- the golden images themselves.

**Every account password is removed** from a range template. The scenario accounts' typed-in
passwords (the lesson password) become the redacted placeholder wherever they appear. A
ported template must have its scenario passwords set again on the new site before a range
is built from it. The preview says how many were removed.

## Importing

On the new site, take these steps first:

1. Set `tn_content_port_trusted_keys` to the origin site's public key and re-run the
   installer.
   - Get the key from the origin's Port content panel ("This site's key"), from
     `GET /content-port/signing-key`, or from `content_port_inprocess.py key`.
   - Several keys can be listed, comma-separated.
   - **With no key set, nothing is imported** (409, fail closed).
   - A malformed entry stops every import: it never silently narrows the list.
2. Port the golden templates the bundle names that this site lacks. The preview lists them
   as "port these templates first".

Then import:

1. **Preview.** Upload the bundle and choose where it lands: your tenant (the default),
   another tenant, or **shared** content. Shared range templates and scenarios land in that
   tenant and public; shared courses land with no tenant. The preview verifies the signature
   first, then shows each item's action, a diff, notes and the missing builds. Nothing is
   stored.
2. **Import.** Each item is created or updated. Every item lands **not yet proven here**:
   - a range built from it must reach Certified on this site;
   - it must be used in a class or an exercise here;
   - the content clock must track it.

   The origin's proof is kept and shown on the chip ("Ported; proven at origin"), but it
   never makes an item proven here. Courses land unpublished. Publish them through a course
   release once they are proven here.

Actions in the preview:

| Action | Meaning |
|---|---|
| create | Nothing here yet. |
| update | Imported before from an older export, and not changed here since. |
| unchanged | This exact content was imported before. |
| conflict, `local_changed` | It was changed here after it was imported. |
| conflict, `local_name` | An item of the same name exists here that no import made. |
| conflict, `older` | The bundle is older than the version imported before (a downgrade). |
| conflict, `visibility` | A range template or scenario here is private and the import asks for shared, or the other way round. Until it is confirmed, what depends on it is skipped too, so a shared course never depends on a private scenario. |
| refused | This site refuses the content the same way the API would: a template that pins networks (`port_group`) or sets `credentials`, YAML that is refused, or a scenario whose objectives cannot be judged. |
| skipped | After import only: a conflict that was not confirmed, or an item whose dependency was not imported. |

**A conflict is replaced only when you confirm it.** Tick it in the preview, or pass
`--overwrite <key>` to the CLI. Unconfirmed conflicts stay as they are.

When a course is updated:

- it keeps its publication state. A new course, lesson or quiz lands unpublished, but an
  update does not unpublish what is live here. The preview warns when the course is
  published here;
- modules are matched by ordinal; modules the bundle lacks are kept and reported;
- each module's authored content (`content_ref`: objectives, topics, lab, the `retired`
  flag) is carried and replaced, with its scenario pointed at the local copy;
- a lesson or quiz is updated in place only if it belongs to the landing tenant and no other
  module uses it. Otherwise a new one is made, so a lesson that other courses share is never
  rewritten. A new lesson or quiz that replaces one takes the replaced one's publication
  state, so Students of a published course do not lose it;
- a quiz that already has Student attempts is kept, and a new quiz replaces it in the module;
- lesson media references and lesson-to-EO links are not carried. An updated lesson keeps
  the ones it has here.

**A bundle is imported once.** Importing it again is refused (409, replay), even after a
partial import. To import again, export a new bundle.

## Air-gapped: the CLI

The CLI runs inside the api image as a named platform administrator (no token), as the
installer runs its other in-process scripts:

```sh
docker compose run --rm --no-deps -w /app -e PYTHONPATH=/app \
  -v <app checkout>:/srcapp:ro -v /srv/port:/port --entrypoint python api \
  /srcapp/scripts/content_port_inprocess.py --admin <upn> <command>
```

| Command | Does |
|---|---|
| `key` | Prints this site's public key, for `tn_content_port_trusted_keys`. |
| `list [--proven-only]` | Lists every course, range template and scenario, proven or why not. |
| `export --item course:<id> [--item ...] --out /port/bundle.json [--force]` | Writes a signed bundle. Exit 3 when refused because something is not proven, with the reasons. Never replaces an existing file without `--force`. |
| `verify /port/bundle.json --public-key <b64>` | Checks the signature and the item hashes **offline**, with no API and no database. Run it on the transfer machine. |
| `preview /port/bundle.json [--tenant <id>] [--shared]` | Shows what importing would do. |
| `import /port/bundle.json [--tenant <id>] [--shared] [--overwrite <key> ...] --yes` | Imports. Without `--yes` it only previews. |

Format **v1** is the bundle of courses, range templates and scenarios. A bundle that carries an
exercise template is **v2** (otherwise identical); a v1 bundle carrying one is refused. A bundle
without exercise templates stays v1, so older sites still read it.

## The bundle and its signature

```json
{"format": "truenorth.content-port.v1|v2", "bundle_id": "<uuid>", "exported_at": "<iso 8601>",
 "origin": {"key_id": "<16 hex>"},
 "items": [{"key": "<item_type>:<uuid>", "item_type": "course|range_template|scenario|exercise_template",
            "origin_id": "<uuid>", "content_sha256": "<hex>", "content": {},
            "depends_on": ["<key>"], "proof": {"proven": true, "conditions": []},
            "golden_builds": [], "removed_passwords": 0}],
 "golden_builds": [{"catalogue_id": "srv2022", "build_id": "...", "checksum": "..."}],
 "signature": {"alg": "Ed25519", "key_id": "...", "public_key": "...",
               "signed_sha256": "...", "value": "..."}}
```

**Signed bytes** are `truenorth-content-port/1\n` followed by the canonical JSON of the
whole document except `signature`. Canonical JSON means:

- keys sorted;
- no whitespace (`,` and `:` as separators);
- UTF-8, with non-ASCII characters written as they are.

This is the range certification export's canonical form (docs/range-certification.md). A
bundle holds no floats, so the form is portable.

**Purpose separation.** Content-port signatures can never be confused with other signatures:

- the context line differs from the feed bundle's (`truenorth/content-feed-bundle/v1`), and
  the range certification export has no context line at all;
- the key is its own: HKDF-SHA256 of `TN_SECRETS_KEY`, info
  `truenorth/content-port-export/v1`;
- `key_id` is the first 16 hex characters of the SHA-256 of the raw public key.

A document signed for one purpose, even by the same key, never verifies as another.

**Reading a bundle.** The checks run in this order, and the first that fails refuses the
bundle:

1. A trusted key must be configured, or nothing is read (409).
2. The bundle must be at most 50 MB (the edge's upload limit for this route).
3. It must be strict JSON: no duplicate keys, no NaN or Infinity, no floats.
4. The signature must verify against a trusted key. The `public_key` the bundle carries is
   never trusted.
5. The document must be exactly a bundle: unknown fields are refused, and every list and
   string is bounded (500 items; YAML up to 1 MB, as everywhere else).
6. The bundle's `origin.key_id` must be the key that signed it.
7. The export time must be no more than a day in the future.
8. Each item's key must be its own type and origin id. A key is `<item_type>:<uuid>`,
   nothing else, and nothing in a bundle is ever used as a file path.
9. Each item's content must be exactly its type's content and must hash to its
   `content_sha256`.
10. Every reference must be listed in `depends_on`, and every dependency must be in the
    bundle.

Refusals are audit-logged (`content_port.bundle_rejected`).

**Replay and downgrade.**

- Each imported bundle id is recorded and unique (`content_port_imports`), so the same
  bundle is refused a second time.
- An older export of an item that was already imported is a conflict (`older`), never a
  silent rollback.
- An imported item remembers its origin and its local fingerprint (`content_port_items`),
  so a later edit on the new site shows up as `local_changed`.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `tn_content_port_trusted_keys` | `""` | The origin sites' public keys (base64 Ed25519), comma-separated. Empty: nothing is imported. |
| `TN_SECRETS_KEY` | (set by the installer) | Its first entry derives this site's content-port signing key. Rotating it changes the key, so the importing site must trust the new key. |
