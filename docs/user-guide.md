# User guide: moved

The single user guide is replaced by one guide per role, checked against the code for v1.2.0
(classes, roll call, Rules of use, booking approval, per-session ranges, attendance,
close-out, the Events and Content calendars, My classes and self-serve labs):

| You are | Read |
|---|---|
| A Student | [guides/05-student-quick-start.md](guides/05-student-quick-start.md) |
| An instructor or observer | [guides/04-instructor-guide.md](guides/04-instructor-guide.md) |
| An administrator | [guides/03-admin-first-day.md](guides/03-admin-first-day.md) |

Where the old sections went:

| Old section | Now |
|---|---|
| Getting started, roles, navigation | Each guide's opening sections |
| Ranges, credentials, Live Class | Instructor guide §5 and §7 |
| Exercises, objectives, scoring, AAR, telemetry search | Instructor guide §12 |
| Teams | Instructor guide §5 (class teams) and §12 (bookings without a class) |
| Templates and golden images | Admin guide §5; [scenarios.md](scenarios.md) and `content/ranges/` for the shipped range templates |
| Troubleshooting | Admin guide, "Troubleshooting" |

A few details not repeated in the guides:

- **Range Designer shortcuts** (focus outside a text field): `Ctrl/Cmd+S` save,
  `Ctrl/Cmd+Z` undo, `Ctrl/Cmd+Y` or `Ctrl/Cmd+Shift+Z` redo, `Ctrl/Cmd+D` duplicate the
  node, `Delete` or `Backspace` delete it.
- There are no app-wide keyboard shortcuts. `Esc` closes dialogs, the notification panel
  and the tour.
- **Supported browsers:** current Chrome, Edge, Firefox and Safari. Use a desktop browser
  for exercises.
## Not in v1.2.0

Earlier versions of this guide described these. The v1.2.0 code does not have them; do not
plan training around them.

- Team scoring, leaderboards and the Student "Team Standings" widget (planned, R2). One score
  per exercise.
- Rating bands (Excellent … Unsatisfactory). Points and percentage only.
- Global keyboard shortcuts and a command palette. Range Designer shortcuts only.
- A floating AI chat assistant. AI is used in specific features: Scenarios → Draft with AI,
  the ARC² Course Studio, and the AAR AI enhance (API).
- Live updates over WebSocket on exercise pages. They poll.
- Interface languages other than English.
