# 05 — Student quick start

Everything you need to take a class or a self-serve lab on TrueNorth Range. Your instructor's
side is in [04-instructor-guide.md](04-instructor-guide.md).

Checked against `main` at `fd4f79a6` (2026-10-27, v1.2.0).

---

## 1. Sign in

1. Open `https://<your-site>/` and press **Sign in with SSO**.
2. Sign in with your **normal domain account**. TrueNorth has no password of its own; change
   your password the usual way for your domain.
3. What happens next:
   - **Your instructor put you on a class roll call:** you go straight in. There is no form
     and no waiting. Use the email address your instructor has for you.
   - **You are on no roll call** (for example, you only want self-serve modules): you see
     **Request access to TrueNorth Range**. Fill in the form and press **Submit request**.
     The **Waiting for approval** page checks every 30 seconds; **Check now** checks
     straight away. If you are declined, you see why and can send a new request.

There is no **Sign out** button inside the app on v1.2.0. Close the browser to end your
session on a shared machine.

## 2. Sign the Rules of use

Before anything else you see the **Rules of use**:

1. Read them to the end. **I agree** stays greyed out until you scroll to the bottom.
2. Press **I agree**, then **Continue**.

You sign again when the rules change and once a year. The rules tell you that **your sessions
may be observed**, including your live screens and consoles.

Next comes a three-step **Welcome to TrueNorth Range**: your details, your training path and a
short tour. You can **Skip for now**.

## 3. My classes

**Learn → My classes** (also linked from the Dashboard) shows your classes and sessions for
the **Week** or **Month**. Use **Today** and the arrows to move around. Each class shows:

| Chip | Meaning |
|---|---|
| **Onboarded** / **Not onboarded** | Whether you have signed in for this class |
| **Rules of use signed** / **not signed** | Sign them before your session |
| **Seat ready** / **Lost seat** | Your place on the class |
| **Range not built / building / ready / paused / failed** | Your range for the current or next session |

The page refreshes itself every minute.

## 4. Join (this is also your attendance)

During a session, press **Join**. The **Your range** panel opens with **Your accounts**: each
machine, its username and its password. **Show password** and **Copy password** are on each
row; showing a password is recorded in the audit log.

**You are marked present** for a session when you open your accounts with **Join** during
that session. Press Join **in every session**, even on day 3 of a week-long class. Your
instructor can correct your attendance.

When **Join** is greyed out, the reason is shown:

| You see | Why |
|---|---|
| "Your range is built shortly before your session." | Not built yet (built 30 minutes before) |
| "Your range is being built." | Building now |
| "Your range is powered off. It opens when your instructor starts it." | **Paused**: your range exists and your work is kept, but its machines are stopped. Join comes back when they are running. |
| "The shared range opens when the session starts." | A collective session: the class shares one range, only during the session |
| "Your range could not be built. Your instructor can rebuild it." | Failed: tell your instructor |

Join gives you accounts, not a console button. Your instructor tells you how to reach the
machines for your class.

**Lost seat.** If you hadn't signed in by **2 hours after your class's first session
started**, you lose your seat. You see "You lost your seat…". Ask your instructor to
reinstate you: it takes them one click.

## 5. Self-serve labs

Some modules have labs you start yourself, without a class:

1. In your course on Moodle, open the activity **Start lab: <module>**. (In TrueNorth:
   **Learn → Learning → Courses → <course> → Open in Moodle**.)
2. The **Lab** page opens. It shows **Building your lab…**, then **Your lab is ready.**
3. Use **Open console**, **Reset** (back to the start; your changes in the lab are lost) or
   **End lab**. Your accounts are on the same page.

**"Labs are busy, next free at HH:MM"** means the lab machines are all in use, by classes and
other labs. Start the lab again from your course at that time. On v1.2.0 the time is shown
in **UTC**.

**"Waiting for a free lab. It starts automatically."** means you are in the queue. Leave the
page open, or **Leave the queue**. You can have only one lab running at a time.

**Idle shutdown.** A lab closes after **4 hours** without use. **Use** means:

- keys, clicks or focus on the lab page;
- the lab page being visible;
- an open console.

A hidden tab nobody touches does **not** count. **15 minutes before**, TrueNorth sends a
notification (the bell, top right in TrueNorth): "Your lab closes soon". Open the lab to keep
it. No lab runs longer than **7 days**. Work you submitted is kept either way.

## 6. Help

- **Help → Support → Report a problem**: describe it, set **How urgent?** (up to **Blocking my
  exercise**), and attach screenshots.
- **Help → Wiki**: how-tos for your site.

---

## Printable checklist: Student

```
Name: ____________________   Class: ____________________   Site: ____________________

[ ] Signed in with my domain account (Sign in with SSO)
[ ] Read the Rules of use to the end → I agree → Continue
[ ] Learn → My classes: my class is listed; chips say Onboarded, Rules of use signed,
    Seat ready
[ ] I know how to reach my machines (ask the instructor)
[ ] EVERY session: My classes → Join (this records my attendance)
[ ] Join greyed out? Read the reason: building / paused / shared range / failed
[ ] Self-serve lab: Moodle → Start lab → Lab page; used within 4 h, or it closes
[ ] "Labs are busy, next free at HH:MM" (UTC) → try again then
[ ] Lost seat? → ask my instructor to reinstate me
[ ] Problem? Help → Support → Report a problem
```
