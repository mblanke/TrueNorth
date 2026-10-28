# ADR 0017: the exercise template record

**Status:** Accepted (owner approved "add the exercise-template record", 2026-10-28)
**Date:** 2026-10-28

## Context

An exercise's objectives, MESL and range binding belong to one `Exercise` on one range, so
the content port (docs/content-port.md) could not carry an exercise between sites. A scenario
is reusable but is only part of the picture.

Owner clarification, 2026-10-28: exercises and courses are separate. **Exercises are
collective training; courses are individual training.**

## Decision

1. **`ExerciseTemplate`** (`control-plane/api/app/exercise_templates/`, table
   `exercise_templates`) holds the reusable parts: name, description, objectives, the MESL
   (without delivery status), the scenario reference, the range-template reference, the teams
   (name, side, minimum and maximum Students), the participant roles and sizing rules (9
   below), the instructor's notes and tags. It is
   **collective only**: there is no individual or `kind` field. It is never attached to a
   course module, a course does not reference it, and it does not touch a course's
   `lab_range`. The only link to courses is the content clock (below) and the port.
2. **Creating an exercise from a template** (`POST /exercise-templates/{id}/exercises`)
   makes a collective `Exercise` and **copies** the objectives and MESL into it. The link
   table `exercise_template_uses` records the template and its `revision`; `exercises` and
   `app.models` are unchanged, and an exercise made by hand has no row. The range must be the
   caller's tenant's and built from the template's range template (or its scenario's), so
   proof means what it says.
3. **Editing never changes exercises already made.** `revision` rises on every edit; the
   exercises keep their own copy and the revision they came from. Deleting a template that
   exercises were made from is refused (409, ADR 0010): they are training record; retire it
   on the content clock instead.
4. **Tenancy follows scenarios and range templates:** `tenant_id` plus `is_public`. Another
   tenant's private template is a 404. A shared one is listed and readable by staff of other
   tenants and usable to make an exercise (its scenario must then be the user's tenant's),
   never changed by them. Only the platform administrator shares one. All endpoints need
   `exercise:create` (admins, instructors), reads included, because the MESL and the notes
   are the answer key (as `collective-exercises`). No new permission.
5. **Optional "Save as template"** (`POST /exercise-templates/from-exercise/{id}`) copies a
   collective exercise's objectives, MESL, scenario and range template; an individual
   exercise is refused.
6. **Content clock (#295):** `exercise_template` is a content-clock type (review dates,
   themes from tags, cycle, staleness). It depends on its scenario and its range template, so
   their staleness reaches it. The worker's sync reads `exercise_templates`.
7. **Proven** (`content_port/proven.py`), computed, never stored. Certified: a range built
   from its range template (the one it names, else its scenario's) reached Certified as the
   template is now. Used: an exercise **made from the template** completed with a passing
   score (70%), **made from the template's current revision and on a range built from the
   range template it runs on now**; an exercise merely on the same range proves nothing, and
   any edit needs a new exercise to prove it again. Fresh: not stale or
   retired on the content clock. A dependency this site cannot read leaves it not proven.
8. **Port:** exports and imports carry `exercise_template` items with their closure: the
   scenario and range template must be proven too (409 lists them). The content is the same
   strict model the API validates, plus the keys of the scenario and range template; no
   tenant, owner, sharing or revision. Import lands **private and not yet proven**, relinked
   to the local copies, under #324's signature, preview, conflict (`local_changed`,
   `local_name`, `older`, `visibility`), dependency-skip and replay rules, with matching per
   landing tenant. A bundle that carries an exercise template is format **v2**; a bundle without one stays
   v1, so older sites still read it. A v1 bundle carrying an exercise template is refused, and
   a format this site does not know is refused with a message to upgrade.

9. **Roles and sizing rules** (owner requirement, 2026-10-28: an exercise request says how
   many people take part in which roles, and that decides how many VMs of what kind are
   created). Additive to the teams. The template stores:
   - `roles`: name, description, optional team, `min_participants`, `max_participants` (0: no
     limit), `default_count`, and a **VM profile**: what each participant of the role gets, as
     `node_type` + `count` (analyst: workstation plus SIEM console; red: attack workstation;
     blue: defender workstation or server; intel: analyst workstation);
   - `scaling_rules` for shared infrastructure: `node_type` + `count`, scaling `per_segment`
     (one sensor per segment), `per_participants` in steps of `per` (targets per N
     participants, optionally of one `role`), or `fixed`.

   A node type is a reference into the range template: it matches a node's `id`, `type`, `os`
   or `name`. **The sizing engine is not here**; the Exercise Studio reads these records. The
   record only guarantees they are consistent (min <= default <= max, unique names, teams and
   roles referenced exist, a step size where one is needed). They travel in the port with the
   rest of the content. **Proven** also requires every node type a role or rule names to
   exist in the range template (a reason under "Range certified"): a profile that points at
   nothing sizes nothing.

10. **Edits reset the review.** Changing a template's scenario, range template, roles or scaling
    rules (in the API, or by an import update) flags its content-clock row stale until it is
    reviewed again, so a changed template is "not yet reviewed".
11. **No leaks across tenants.** A shared template (or scenario) never names another tenant's
    private scenario or range template: not in the dependency list, nor in any reason, and the
    dependency's status is not shown. The export refusal does not name another tenant's content.
12. **Concurrency and caps.** Delete, edit, create-exercise and an import update lock the
    template row (`SELECT ... FOR UPDATE`, read as it is now), so two writers never share a
    revision number. A template is validated, on create, update and import, against
    operator-settable caps. Exactly what is counted:
    - a role's participants are its `max_participants`, or, **when it has no maximum (0), its
      `default_count`**; the template's total participants is their sum
      (`TN_EXERCISE_TEMPLATE_MAX_PARTICIPANTS`, default 500);
    - each participant's VMs are the sum of its VM profile counts
      (`..._MAX_VMS_PER_PARTICIPANT`, default 20);
    - **total VMs** (`..._MAX_VMS`, default 1000) is the worst case of the roles plus the
      scaling rules:
      - roles: participants x VMs per participant, summed;
      - `fixed` rule: its `count`;
      - `per_participants` rule: `count x ceil(people / per)`, where people is the named
        role's participants (as above) or, with no role, the template's total participants;
      - `per_segment` rule: `count x MAX_SEGMENTS`, a conservative bound on the segments a
        range may have (`TN_EXERCISE_TEMPLATE_MAX_SEGMENTS`, default 10).

    An import re-checks that the scenario and range template it relinks to still agree, and
    skips the template with a reason if not. The platform administrator reads no other tenant's
    private names either (reasons, dependency list, export refusal); the redaction matches the
    whole name, whatever its case.

## Consequences

- One migration (`9d4e7a2b5c18`, chained on `7c1d2e9f4a60`) adds two tables and writes no
  rows.
- The shared Angular client `ExerciseTemplatesApiService`; pages under Exercises
  (`/exercises/templates`).
- Exercise templates are not offered for courses; individual training stays in courses.
