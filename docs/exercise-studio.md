# Exercise Studio

> Status: design (ADR 0016, 2026-10-28). Nothing here is built yet; this is the flow people
> will see. Decisions and reasons: `docs/adr/0016-exercise-studio.md`.

Exercise Studio does for **collective exercises** (teams, roles, one shared range) what ARC²
does for courses: you describe what you want, it drafts everything in stages, checks it,
proves it runs, and an admin decides. It never touches courses or individual labs.

## For instructors and event planners

1. **Ask.** In the Authoring hub, open Exercise Studio and choose New request. Write what
   you want ("a two-day red-versus-blue exercise on a ransomware intrusion") and fill in
   **how many people take part, per role**: for example 14 analysts, 6 red team, 14 blue
   team, 4 intel. Say if live attacks are wanted (only offered where the site allows them).
2. **Answer questions.** If the numbers are missing or do not fit the idea (a red-versus-blue
   exercise with no red team, a role the storyline needs with nobody in it), the run stops at
   **Needs input** and asks. Answer in the web app; it carries on.
3. **Check the outline.** You see the objectives, teams, roles, the sizing and the phases.
   Accept it or send feedback.
4. **Read the capacity report.** It says, for example: "Needs 62 VMs, 148 vCPU, 396 GB RAM,
   5.1 TB disk. The site can fit it." or "The site cannot fit it; the biggest team size that
   fits is 38 participants (analyst 14, red 6, blue 14, intel 4)."
5. **Wait for the dry run and the admin.** The Studio drafts the scenario, the event list
   (MESL), the injects, scoring, your instructor pack and the debrief template, checks them,
   then builds a small copy of the range, plays the injects with simulated players, and tears
   it down. If something fails it fixes it itself, up to three times, then hands over to an
   admin. You can watch the stage list at any time.
6. **Use it.** Once an admin has promoted it, the exercise template appears for your class
   sessions. When you book a collective session, the headcount per role is filled in from the
   roll call; change it and the range is sized again. If the size (so the capacity) changes,
   the booking goes back to approval. Students are placed in teams **and roles**.

## For admins

- **Review queue.** Each finished request shows: the outline, the generated material, the
  QA findings, the **dry-run report** (pass or fail: did the range build and certify as
  isolated, did every inject fire, can every objective be scored, can a silent player score
  nothing, did teardown finish clean), and the capacity report.
- **Accept the preview, then Promote.** Two clicks, both audited. Nothing is created in the
  tenant before Promote, and Promote itself books and starts nothing.
- **Live attacks need a second person.** If an exercise contains live inject scripts, a
  different admin with the live-approval permission must approve those exact scripts on their
  own screen. Live attacks are built only from a reviewed library of pre-approved building
  blocks, in a declared order; there are no free-form attack scripts, and a new technique
  enters by a reviewed addition to the library, not by the generator. Live injects run only on a range certified isolated, with a kill switch in the
  Ops Center, and only where the site has turned live on. Live is off by default.
- **Drafted ranges.** If the request needed a range that does not exist yet, the Studio drafts
  one and lists the golden images it needs and which ones exist. The draft is unusable until
  the images are built and promoted at the work site and a range built from it is certified.
  Until then the exercise is Draft and the dry run is deferred.
- **Proven and export.** After a passing dry run the exercise shows **Proven by dry run**.
  Proven exercises export through **Port content** as a signed bundle. Exercises on a drafted
  range cannot be exported until they are proven.
- **Stuck runs.** Three content failures send a run to takeover: you fix the files by hand
  and resume. Capacity or infrastructure problems wait and can be retried; they do not count
  as failures.

## What it will not do

It will not provision, commit, import or call anything outside its sandbox. It writes no
executable text: live attacks are sequences of reviewed actions with limits. It never targets
anything outside the range, never leaves anything behind after teardown, never uses real
malware or real data, and never touches courses. The full list is in ADR 0016, "What the
generator must never produce".
