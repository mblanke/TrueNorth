# TrueNorth Range — User Guide

> End-user documentation for instructors, students, and observers covering every workflow from first login through after-action review.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Dashboard Overview](#dashboard-overview)
- [Managing Ranges](#managing-ranges)
- [Running Exercises](#running-exercises)
- [Working with Scenarios](#working-with-scenarios)
- [Objectives and Scoring](#objectives-and-scoring)
- [After Action Reports (AAR)](#after-action-reports-aar)
- [Telemetry and Event Viewer](#telemetry-and-event-viewer)
- [Team Management](#team-management)
- [Templates](#templates)
- [AI Assistant](#ai-assistant)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Frequently Asked Questions](#frequently-asked-questions)

---

## Getting Started

### First Login

TrueNorth does not issue you a password. You sign in with your **existing domain
credentials** — the same username and password you use for everything else — and
the range checks them against Active Directory.

1. Navigate to `https://your-truenorth-instance.com` in a modern browser
   (Chrome 120+, Firefox 120+, Edge 120+, Safari 17+)
2. Click **Sign In** — you are redirected to the Keycloak login page
3. Enter your **domain** credentials. If your organisation requires multi-factor
   authentication, you are challenged for it here, as usual.
4. What happens next depends on whether you already have a TrueNorth account:
   - **You have one** — you land on the dashboard.
   - **You do not** — you land on a short **access request** form instead. This
     is expected on a first login and is not an error.

### Requesting access

Authenticating proves who you are. It does not, by itself, create an account —
an instructor admits you.

The form already knows your name, email and directory details, and those fields
are read-only: they come from the directory, and editing them here would only
put the two out of step. It asks for what the directory does not know — your
rank, unit, callsign, nation, time zone, the qualification you are joining, and
your cohort or serial number.

Submit it and you are on a waiting screen that checks for you every 30 seconds.
You can leave it open or sign out and come back. If your request is declined you
will see the reason and can submit a new one.

### Getting started once approved

The first time you sign in with a live account you get three short steps:
confirm the details you already gave, confirm the training path you have been
enrolled on, and a quick tour of where things live. You will not be asked again.

> Full detail, including the instructor's side of the approval queue, is in
> [onboarding.md](onboarding.md).

### Understanding Your Role

Your role determines what you can see and do:

| Feature | Admin | Instructor | Student | Observer | Range Ops |
|---------|:-----:|:----------:|:-------:|:--------:|:---------:|
| View dashboard | X | X | X | X | X |
| Create ranges | X | X | | | X |
| Provision/destroy ranges | X | X | | | X |
| Create exercises | X | X | | | |
| Start/pause/complete exercises | X | X | | | |
| Participate in exercises | X | X | X | | |
| View telemetry | X | X | X | X | |
| Acknowledge objectives | X | X | X | | |
| Generate AARs | X | X | | | |
| View AARs | X | X | X | X | |
| Approve access requests | X | X | | | |
| Manage users | X | | | | |
| Manage tenants | X | | | | |
| View audit logs | X | | | | |

Your role is assigned when your access request is approved. Directory group
membership *suggests* a role to the approver but does not set it — being in
`TN-Platform-Admins` does not make you an administrator here.

> The interface says **Trainee**; the underlying role value is `student`. They
> are the same thing.

### Navigation

The main navigation sidebar includes:

- **Dashboard** — Overview of active ranges, exercises, and system health
- **Ranges** — Create, view, and manage cyber ranges
- **Exercises** — Create and run training exercises
- **Templates** — Browse and manage range templates
- **Scenarios** — Browse and manage attack scenarios
- **Reports** — Access after-action reports
- **Teams** — Manage teams and team membership
- **Admin** — (admin only) User management, tenants, audit logs

---

## Dashboard Overview

The dashboard provides an at-a-glance view of your training environment:

### Instructors and Admins

| Widget | Description |
|--------|-------------|
| **Active Ranges** | List of currently provisioned ranges with state indicators |
| **Running Exercises** | Exercises in progress with elapsed time and progress bars |
| **Recent Activity** | Timeline of recent actions (provisions, exercise starts, completions) |
| **Quick Actions** | Buttons for common tasks: New Range, New Exercise, View Reports |
| **System Health** | Status indicators for core services (API, database, search) |

### Students

| Widget | Description |
|--------|-------------|
| **My Exercises** | Exercises you are participating in |
| **Current Objectives** | Active objectives with status (pending/achieved) |
| **Recent Scores** | Your recent exercise scores and ratings |
| **Team Standings** | Leaderboard for team-based exercises |

### Observers

| Widget | Description |
|--------|-------------|
| **Active Exercises** | Read-only view of running exercises |
| **Score Summary** | Aggregate scoring across teams and individuals |
| **Event Feed** | Real-time feed of exercise events |

---

## Managing Ranges

### What Is a Range?

A **range** is a collection of virtual machines that simulates a network environment. Ranges are provisioned from templates and can host one or more exercises.

### Range States

```
created → provisioning → ready → running → stopped → destroying → destroyed
                                  ↑         ↓
                                  +---------+
```

| State | Description | Actions Available |
|-------|-------------|-------------------|
| `created` | Range defined but not provisioned | Provision, Edit, Delete |
| `provisioning` | VMs being created (Terraform running) | Wait, Cancel |
| `ready` | VMs running, range available | Start Exercise, Stop, Destroy |
| `running` | Active exercise in progress | View, Monitor |
| `stopped` | VMs halted, state preserved | Resume, Destroy |
| `destroying` | VMs being torn down | Wait |
| `destroyed` | All VMs removed | Delete record |
| `failed` | Provisioning or operation failed | Retry, Destroy, Delete |

### Creating a Range

1. Navigate to **Ranges** → **New Range**
2. Fill in the form:
   - **Name** — Descriptive name (e.g., "SOC Training Lab - Cohort 12")
   - **Description** — Optional notes about the range's purpose
   - **Template** — Select from available templates (see Templates section)
   - **VM Count** — Number of VMs (within template limits)
3. Click **Create**
4. The range is created in `created` state

### Provisioning a Range

1. From the Ranges list, click on your range
2. Click **Provision** in the action bar
3. Confirm the provisioning request
4. The range moves to `provisioning` state
5. A spinner and progress indicator show provisioning status
6. WebSocket updates provide real-time state changes
7. Once complete, the range moves to `ready` state

**Typical provisioning times:**
| Template Size | VMs | Time |
|--------------|-----|------|
| Small Enterprise | 3 | 3-5 min |
| Medium Enterprise | 8 | 8-12 min |
| Large Enterprise | 20+ | 15-25 min |

### Batch Provisioning

Admins and Range Ops can provision multiple ranges at once:

1. Navigate to **Ranges** → **Batch Provision**
2. Select ranges to provision (checkboxes)
3. Click **Provision Selected**
4. Confirm the batch operation
5. All selected ranges begin provisioning in parallel

### Monitoring a Range

The range detail page shows:

- **Status** — Current state with timestamp
- **VM List** — All VMs with IPs, hostnames, OS, and status
- **Network Diagram** — Visual topology of the range network
- **Resource Usage** — CPU, memory, and disk for each VM
- **Exercises** — Exercises associated with this range
- **Telemetry** — Link to telemetry viewer for this range

### Stopping a Range

1. Click **Stop** on the range detail page
2. Confirm the stop action
3. VMs are halted (state preserved on disk)
4. Use **Resume** to restart VMs later

### Destroying a Range

1. Click **Destroy** on the range detail page
2. Type the range name to confirm deletion
3. All VMs and associated resources are removed
4. Telemetry data is retained per retention policy

---

## Running Exercises

### What Is an Exercise?

An **exercise** is a training session conducted on a range. It uses a scenario to inject attack events and defines objectives for trainees to complete.

### Exercise States

```
pending → running → paused → completed
                      ↑
                      ↓
                    cancelled
```

| State | Description |
|-------|-------------|
| `pending` | Exercise created, not yet started |
| `running` | Exercise active, scenario events firing |
| `paused` | Exercise paused — timer and events frozen |
| `completed` | Exercise finished — scored and closed |
| `cancelled` | Exercise aborted without scoring |

### Creating an Exercise

1. Navigate to **Exercises** → **New Exercise**
2. Fill in the form:
   - **Name** — Exercise name (e.g., "Incident Response Drill 2026-Q1")
   - **Range** — Select a provisioned range (must be in `ready` state)
   - **Scenario** — Select the attack scenario to run
   - **Teams** — Optionally assign teams
3. Click **Create**
4. The exercise is created in `pending` state

### Starting an Exercise

1. From the exercise detail page, click **Start**
2. Confirm the start action
3. The exercise moves to `running` state
4. The scenario timeline begins executing
5. Real-time updates stream via WebSocket to all participants

**What happens when you start:**
- Scenario Engine begins executing the timeline
- Injectors fire events according to the timeline
- Telemetry flows into OpenSearch
- Objectives become available for completion
- Exercise timer starts

### Pausing an Exercise

1. Click **Pause** on the exercise detail page
2. The scenario timeline freezes
3. All participants see the paused state
4. Resume with the **Resume** button

### Completing an Exercise

Exercises can be completed in two ways:

**Manual completion:**
1. Click **Complete** on the exercise detail page
2. Final scores are calculated
3. After-Action Report can be generated

**Automatic completion:**
- When all required objectives are achieved
- When the exercise duration expires

### Viewing Exercise Progress

The exercise dashboard shows:

| Panel | Description |
|-------|-------------|
| **Timeline** | Visual timeline of scenario events (past and upcoming) |
| **Objectives** | List of objectives with status (pending/achieved/missed) |
| **Score** | Current points earned / total available |
| **Event Feed** | Real-time feed of scenario events and trainee actions |
| **Participants** | Connected users and their activity |

---

## Working with Scenarios

### Browsing Scenarios

Navigate to **Scenarios** to see available scenarios:

| Column | Description |
|--------|-------------|
| **Name** | Scenario display name |
| **Difficulty** | Beginner / Intermediate / Advanced / Expert |
| **Duration** | Expected exercise duration |
| **MITRE ATT&CK** | Mapped techniques |
| **Tags** | Searchable tags |
| **Version** | Scenario version number |

### Scenario Details

Click a scenario to see:

- **Description** — Full scenario narrative
- **Attack Timeline** — Sequence of events with timing
- **Objectives** — What trainees must accomplish
- **MITRE ATT&CK Mapping** — Techniques covered
- **Prerequisites** — Required range template and sensors
- **Difficulty Rating** — With recommended audience

### Creating Scenarios (Instructors)

See the [Scenario Authoring Guide](scenarios.md) for complete YAML schema and authoring instructions.

**Quick path:**
1. Navigate to **Scenarios** → **New Scenario**
2. Fill in name, version, and optional fields
3. Paste or upload YAML content
4. Click **Create** — YAML is validated on submission
5. If validation fails, error details are shown

---

## Objectives and Scoring

### How Objectives Work

Each exercise has objectives defined by its scenario. Objectives are tasks trainees must complete:

- **Detect** — Identify attack activity (validated by search queries)
- **Respond** — Perform a response action (validated by ack or search)
- **Contain** — Contain the threat (validated by ack)
- **Report** — Submit written analysis (validated by file upload)
- **Deliverable** — Upload an artifact (validated by file check)

### Acknowledging Objectives

For objectives that require manual acknowledgment:

1. Go to the exercise detail page → **Objectives** tab
2. Find the objective to acknowledge
3. Click **Acknowledge**
4. Optionally add evidence (text description or file upload)
5. Click **Submit**
6. The validator checks the acknowledgment
7. If valid, the objective is marked as **Achieved** and points are awarded

### Auto-Validated Objectives

Some objectives are automatically validated:

- **OpenSearch query validators** continuously check for matching events
- When matching events are found, the objective is auto-achieved
- A notification is sent to the trainee and instructor

### Understanding Scores

| Element | Description |
|---------|-------------|
| **Points Earned** | Sum of achieved objective points |
| **Points Available** | Total possible points |
| **Percentage** | `(earned / available) * 100` |
| **Rating** | Excellent (90%+), Good (75-89%), Satisfactory (60-74%), Needs Improvement (40-59%), Unsatisfactory (<40%) |

---

## After Action Reports (AAR)

### What Is an AAR?

An After-Action Report (AAR) is an AI-generated analysis of exercise performance. It summarizes what happened, what was detected, what was missed, and provides recommendations.

### Generating an AAR

1. Complete the exercise (score must be calculated)
2. Navigate to the exercise detail page
3. Click **Generate AAR**
4. The AI Orchestrator analyzes:
   - Scenario timeline vs. trainee detections
   - Time-to-detect for each objective
   - Overall score and rating
   - Gaps in detection and response
5. Processing takes 30-60 seconds
6. The AAR is saved and available for viewing

### Viewing an AAR

AARs are available in two formats:

**JSON format** (via API):
```
GET /exercises/{id}/aar
```

**HTML format** (rendered in browser):
```
GET /exercises/{id}/aar/html
```

### AAR Contents

| Section | Description |
|---------|-------------|
| **Executive Summary** | High-level overview of exercise results |
| **Scenario Recap** | What attack was simulated |
| **Timeline Analysis** | Chronological event-by-event analysis |
| **Objective Results** | Each objective with achieved/missed status |
| **Detection Gaps** | Attacks that were not detected |
| **Response Assessment** | Quality and timeliness of responses |
| **Recommendations** | Specific improvement suggestions |
| **Score Breakdown** | Points by objective category |
| **MITRE ATT&CK Coverage** | Heat map of detected vs. missed techniques |

---

## Telemetry and Event Viewer

### Accessing Telemetry

1. From a range or exercise page, click **View Telemetry**
2. The event viewer opens with the range's telemetry data

### Event Viewer Features

| Feature | Description |
|---------|-------------|
| **Search Bar** | Full-text search across all events |
| **Time Range** | Filter events by time window |
| **Source Filter** | Filter by event source (Sysmon, Zeek, DNS, etc.) |
| **Event Type Filter** | Filter by event type (process, network, file, etc.) |
| **Host Filter** | Filter by hostname |
| **Column Selector** | Choose which fields to display |
| **Event Detail** | Click any event to see full JSON |
| **Export** | Download matching events as CSV or JSON |

### Common Queries

| Purpose | Query |
|---------|-------|
| Find phishing emails | `event.action:email_received AND email.has_attachment:true` |
| Find PowerShell execution | `process.name:powershell.exe` |
| Find DNS queries to external domains | `event.action:dns_query AND NOT dns.question.name:*.corp.local` |
| Find new user accounts | `event.action:user_created` |
| Find lateral movement | `event.action:logon AND logon.type:10` |
| Find data exfiltration | `event.action:http_request AND destination.port:443 AND NOT destination.ip:10.*` |

---

## Team Management

### Creating a Team

1. Navigate to **Teams** → **New Team**
2. Enter a team name
3. Click **Create**

### Adding Members

1. Click on a team name
2. Click **Add Member**
3. Search for a user by name or email
4. Select the user and click **Add**

### Team Exercises

When assigning teams to exercises:

1. Create an exercise
2. In the exercise settings, assign one or more teams
3. Each team member can participate
4. Scoring can be individual or team-based
5. Leaderboards show team rankings

---

## Templates

### What Is a Template?

A **template** defines the virtual infrastructure for a range — the VMs, their operating systems, network topology, and installed software. Templates are the blueprints from which ranges are provisioned.

### Browsing Templates

Navigate to **Templates** to see available templates:

| Column | Description |
|--------|-------------|
| **Name** | Template name |
| **VM Count** | Number of VMs in the template |
| **Description** | Template purpose and contents |
| **Public** | Whether available to all tenants |
| **Created** | When the template was created |

### Template Details

Click a template to see:

- **VM Definitions** — List of VMs with OS, role, and specs
- **Network Topology** — Visual diagram of network layout
- **Software** — Pre-installed security tools and services
- **Requirements** — Minimum host resources needed

### Standard Templates

| Template | VMs | Description |
|----------|-----|-------------|
| Small Enterprise | 3 | DC, 1 workstation, 1 server |
| Medium Enterprise | 8 | DC, 4 workstations, 2 servers, FW |
| Large Enterprise | 20+ | Full corporate: DC, workstations, servers, DMZ, SIEM |
| SOC Analyst Lab | 5 | SIEM-focused: ELK stack, sample data sources |
| Incident Response | 10 | IR toolkit: Forensics workstation, compromised hosts |

---

## AI Assistant

### Overview

TrueNorth Range includes an AI-powered assistant that helps with:

- **Scenario Analysis** — Understanding attack chains
- **AAR Generation** — Automated exercise analysis
- **IOC Context** — Looking up indicators of compromise
- **Guidance** — Training tips and recommendations

### Using the AI Assistant

1. Click the **AI Assistant** icon in the bottom-right corner
2. Type your question or request
3. The assistant responds with contextual help

### Example Prompts

| Prompt | Expected Response |
|--------|------------------|
| "Explain this attack chain" | Analysis of the current scenario's techniques |
| "What should I look for next?" | Hints based on scenario progress |
| "What is T1059.001?" | MITRE ATT&CK technique explanation |
| "How do I detect lateral movement?" | Detection strategies and queries |

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl + K` | Open command palette |
| `Ctrl + /` | Open search |
| `Ctrl + N` | New (context-dependent: range, exercise, etc.) |
| `Ctrl + S` | Save current form |
| `Ctrl + Enter` | Submit / confirm action |
| `Esc` | Close modal / cancel |
| `?` | Show keyboard shortcuts help |
| `G then D` | Go to Dashboard |
| `G then R` | Go to Ranges |
| `G then E` | Go to Exercises |
| `G then T` | Go to Templates |
| `G then S` | Go to Scenarios |

---

## Frequently Asked Questions

### General

**Q: What browsers are supported?**
A: Chrome 120+, Firefox 120+, Edge 120+, Safari 17+. Chromium-based browsers recommended for best performance.

**Q: Can I use TrueNorth Range on a mobile device?**
A: The interface is responsive but optimized for desktop. Exercise participation requires a desktop browser.

**Q: How do I reset my password?**
A: Click "Forgot Password" on the login page, or contact your administrator.

### Ranges

**Q: How long does provisioning take?**
A: 3-25 minutes depending on template size (see provisioning times table above).

**Q: Can I keep a range running indefinitely?**
A: Ranges consume resources while running. Your administrator may enforce auto-stop policies.

**Q: What happens to my data when a range is destroyed?**
A: VMs and local data are deleted. Telemetry in OpenSearch and reports in MinIO are retained per your organization's retention policy.

**Q: My range is stuck in "provisioning" — what do I do?**
A: Wait up to 30 minutes for large templates. If still stuck, contact your administrator to check the provisioning task.

### Exercises

**Q: Can I pause and resume an exercise?**
A: Yes. Click **Pause** to freeze the scenario timeline and **Resume** to continue.

**Q: What happens if I disconnect during an exercise?**
A: The exercise continues running. Reconnect to see current state. WebSocket will automatically reconnect.

**Q: Can multiple people participate in the same exercise?**
A: Yes. All team members can view and interact with the same exercise simultaneously.

**Q: How are objectives validated?**
A: Three methods: (1) automatic OpenSearch query matching, (2) manual acknowledgment by trainee, (3) file upload verification.

### Scoring

**Q: Can I improve my score after an exercise completes?**
A: No. Scores are final at exercise completion.

**Q: How is the team score calculated?**
A: Team score is the sum of all objectives achieved by any team member.

**Q: What is a passing score?**
A: Per your organization's policy. The default rating system considers 60%+ as "Satisfactory."

### Troubleshooting

**Q: I see "403 Forbidden" when accessing a resource.**
A: Your role does not have permission for that action. Contact your administrator.

**Q: Telemetry viewer shows no events.**
A: Ensure the exercise has started and events have been injected. Check the time range filter.

**Q: AAR generation fails.**
A: Ensure the exercise is completed (not just paused). Check that the AI Orchestrator is healthy.

**Q: WebSocket disconnects frequently.**
A: Check your network connection. Ensure your browser allows WebSocket connections. Contact your administrator if the issue persists.