# TrueNorth guides (v1.2.0)

The guides for people who install, run and use TrueNorth, one per role. Read them in this
order; each ends with a one-page checklist to print.

| # | Guide | For | Covers |
|---|---|---|---|
| 01 | [Site prerequisites](01-site-prerequisites.md) | The site's infrastructure, network and directory teams; the installer | Hardware by role (the 9U kit as a worked example), VLANs, switch and firewall, DNS, CA, AD, the accounts the site provides |
| 02 | [Install guide](02-install-guide.md) | Whoever installs TrueNorth | Building the Ubuntu VM, verifying the signed release, running the installer, first sign-in. Appendices: air-gapped install, a minimal AD DC, a GPU AI node, building golden templates from ISO |
| 03 | [Admin first day](03-admin-first-day.md) | Platform and tenant administrators | The first day after the install |
| 04 | [Instructor guide](04-instructor-guide.md) | Instructors and content authors | Classes, courses, ranges and exercises |
| 05 | [Student quick-start](05-student-quick-start.md) | Students | Signing in and taking a course |
| 06 | [Operations runbook](06-operations-runbook.md) | Whoever runs the platform | Upgrades, backup and restore with the drill, porting golden templates, common failures, the isolation check, feeds, the AD status sync |

Conventions in every guide: `<angle-brackets>` are placeholders for the site's own values
(no guide holds a real address, password or key); **[DECISION]** marks a step where a
person must choose and record the choice; terms such as *range*, *golden template* and
*Student* are used as [`docs/GLOSSARY.md`](../GLOSSARY.md) defines them.

The guides are the path for one site. The references they point into stay the source of
truth for detail: [`install/README.md`](../../install/README.md) (every installer variable),
[`docs/operations.md`](../operations.md) (alerts, range leases), [`docs/runbooks/`](../runbooks/),
and [`docs/release.md`](../release.md) (how releases are made and verified).

## PDF copies

`scripts/build_guides_pdf.sh` renders every guide in this directory to PDF with pandoc, for
printing or for a transfer kit:

```bash
scripts/build_guides_pdf.sh            # writes build/guides-pdf/*.pdf
scripts/build_guides_pdf.sh <out-dir>  # or somewhere else
```

It needs `pandoc` and a PDF engine on the machine (on Ubuntu:
`sudo apt-get install -y pandoc texlive-xetex texlive-fonts-recommended`; on macOS:
`brew install pandoc` and a TeX distribution such as BasicTeX). Without pandoc it says so
and exits non-zero. The release workflow does not build the PDFs: run the script when you
cut the documentation set, and attach the PDFs where they are needed.
