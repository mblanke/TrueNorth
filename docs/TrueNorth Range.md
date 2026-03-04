TrueNorth Range
Overview

Design a modular, Docker-based cyber training platform combining: a lightweight LMS (xAPI/cmi5 compatible), a scenario builder/playback engine, and a live-fire red/blue/purple team range (with tools like HELK and Velociraptor). The GUI will use React with Google’s Material Design (via Material UI) for a polished, modern look. We’ll base the code layout on the existing dev-backbone-template structure (separate frontend/backend/services) and orchestrate everything with Docker Compose for simplicity. Each component will run in its own container: frontend, backend, database, LMS/LRS, scenario engine, AI orchestrator, etc. This spec outlines the architecture, directory layout, Docker Compose design, and step-by-step scaffolding commands for each service.

Architecture & Components

Frontend (React + Material UI): A single-page app using Material UI components (Google’s Material Design library) for a clean interface. It will provide dashboards for learners, scenario creation, and monitoring.

Backend API (Node/Express or similar): A RESTful service supporting the frontend, handling user auth, content delivery, and orchestration calls. It will expose endpoints for LMS actions, scenario management, and launching attacks/defenses.

Database (PostgreSQL or similar): Stores user profiles, scenario definitions, and other relational data. It will also hold LMS course metadata (if needed). Separate from the Learning Record Store (LRS).

Learning Management Service: Integrates an xAPI Learning Record Store (LRS) to track learner activity. We’ll implement or embed an LRS (e.g. an open-source SQL LRS) and ensure cmi5 compliance. cmi5 is an xAPI “profile” defining how LMS launches and tracks content, ensuring interoperability. By using cmi5, the LMS can launch lessons and send rich xAPI statements about completions, scores, etc.. We will containerize the LRS so it can store all xAPI statements. (For example, SQL LRS is open-source and “lightweight, containerized”.)

Scenario Builder/Playback Module: A service (with frontend UI) allowing instructors to create and configure training scenarios (network topology, vulnerabilities, attack sequences, etc.). Scenarios are stored (e.g. as JSON/YAML) in the repository/database. The Scenario Engine (another service) can then play back these scenarios: orchestrating VMs/containers, injecting traffic or malware, and recording events.

Live-Fire Range (Red/Blue/Purple Team): A dedicated environment for attack/defense exercises. It includes open-source tools: e.g. HELK (Hunting ELK) for SIEM/threat hunting and Velociraptor for endpoint monitoring/DFIR. HELK and Velociraptor can run in their own containers (or VMs) as part of the range. These allow the Blue Team to detect/respond and Red Team to execute realistic attacks. Live-fire exercises simulate real attacks; e.g. a "deep technical Red vs Blue exercise" per realistic scenario. We will automate setup of these tools (e.g. via Docker or scripts) so the range can spin up on demand.

AI Orchestrator (Code Assistants): A service that integrates AI coding/analysis agents (Claude Code, Opus, or OpenAI Codex) via APIs or CLI interfaces. This AI agent can help generate code snippets, suggest scenario steps, or review playbooks. We will use an open-agent standard (such as Replit’s Agents.md) to script tasks. Note: OpenAI Codex is open source (so fully customizable), while Claude Code offers advanced features (multi-agent, hooks) for deep automation. The orchestrator service will accept instructions (e.g. “generate a detection rule for this scenario”) and communicate with the chosen AI model, returning results to the user or storing them.

Each component runs in its own Docker container. We use docker-compose to define and link services. Docker Compose is ideal here because it’s easy for small-to-medium multi-service apps: “you can containerize them and define them with Compose pretty quickly, resulting in a full-fledged running application”. It simplifies development: we edit code and simply docker-compose up to rebuild and launch the entire stack. Compared to Kubernetes, Compose has a shallower learning curve for a project of this size.

Docker Compose Design

We will have (at minimum) these services in docker-compose.yml:

version: '3.8'
services:
  frontend:
    build: ./frontend
    ports:
      - "3000:3000"

  backend:
    build: ./backend
    ports:
      - "4000:4000"
    depends_on:
      - db
      - lrs

  db:
    image: postgres:15
    restart: unless-stopped
    environment:
      - POSTGRES_USER=appuser
      - POSTGRES_PASSWORD=secret
      - POSTGRES_DB=cyberdb
    volumes:
      - db-data:/var/lib/postgresql/data

  lrs:  # Learning Record Store for xAPI
    image: gitdev/sql-lrs:latest  # Example open-source LRS
    ports:
      - "8000:8000"
    restart: unless-stopped

  scenario-engine:
    build: ./scenario-engine
    ports:
      - "5000:5000"
    depends_on:
      - db

  ai-orchestrator:
    build: ./ai-orchestrator
    ports:
      - "6000:6000"
    environment:
      - MODEL=claude-code
    depends_on:
      - backend

volumes:
  db-data:

Each service folder (./frontend, ./backend, etc.) will contain a Dockerfile and code. The Compose file above defines ports and dependencies. For example, the backend API depends on db (Postgres) and lrs (the Learning Record Store) so those start first. The lrs image (like SQL LRS) is containerized and open-source. We’ll extend this as needed (adding services for scenario-builder UI, Helk, Velociraptor, etc.).

Directory Layout

Following the dev-backbone-template style, the project structure will look like:

project-root/
├── frontend/           # React app (Material UI)
│   ├── public/
│   ├── src/
│   └── Dockerfile
├── backend/            # Node/Express API
│   ├── src/
│   └── Dockerfile
├── lms/                # LMS and LRS integration (maybe Node service or Moodle custom)
│   ├── src/
│   └── Dockerfile
├── scenario-builder/   # Web UI for building scenarios
│   ├── public/
│   ├── src/
│   └── Dockerfile
├── scenario-engine/    # Service that runs scenarios
│   ├── src/
│   └── Dockerfile
├── ai-orchestrator/    # AI agent integration (e.g. Python/Node service)
│   ├── src/
│   └── Dockerfile
├── db/                 # Database initialization/scripts (if any)
│   └── init.sql
├── docker-compose.yml  # Multi-service orchestration
└── README.md           # Project kickoff (this spec)

This layout mirrors the modular approach of the dev-backbone-template (separate folders for each service) and includes placeholders for all major components. Each sub-project can have its own language/runtime and Dockerfile.

Scaffolding Steps

Initialize Git repo & base template:

Clone or copy the dev-backbone-template structure into project-root.

Ensure a docker-compose.yml exists at the root (start with a blank version as above).

Frontend setup (React + Material UI):

In frontend/, run npx create-react-app . --template cra-template-pwa (or similar) to scaffold.

Install Material UI: npm install @mui/material @emotion/react @emotion/styled.

Scaffold basic pages (Login, Dashboard, Scenario Builder UI) using Material UI components.

In frontend/Dockerfile, use node:18-alpine, copy app, and set up to serve on port 3000.

Backend setup (Node/Express):

In backend/, initialize with npm init -y and install Express and any ORM (e.g. Sequelize/TypeORM) for Postgres.

Create basic server code (src/index.js) with routes for users, scenarios, etc.

Add endpoints for LMS data: e.g. endpoints to post xAPI statements to the LRS.

In backend/Dockerfile, use node:18-alpine, copy code, install deps, and listen on port 4000.

Database (PostgreSQL):

Use the postgres:15 image in Compose.

Create initial schema via db/init.sql and docker-compose volume mount.

Define tables for users, courses, scenario definitions, etc.

In backend code, connect to Postgres (use environment variables for credentials).

LMS/LRS integration:

Choose an open-source LRS (e.g. SQL LRS as in Compose example).

Configure the LRS (e.g. initial admin, DB connection).

Ensure cmi5 compliance: our LMS module must launch content via cmi5 launch URLs. This involves building or embedding a small LMS system. For simplicity, you might implement minimal LMS endpoints to register learners, create courses, and redirect to assignable units (cmi5 Launch).

Example scaffolding: you could create a lms/ Node service that wraps the LRS and handles cmi5 flows (parsing cmi5 XML, issuing launch tokens, etc.).

Confirm that the LMS uses xAPI to record completions. (cmi5 “has statements specifically defined for completion, mastery”).

Scenario Builder (UI):

In scenario-builder/, set up another React app (or integrate into frontend) for instructors to define scenarios.

Provide UI forms to specify network configs, VMs, attacker tools, objectives.

Scenarios are stored via the backend API to the database.

Scenario Engine:

In scenario-engine/, create a service (Node or Python) that can take a scenario definition and execute it. This might involve using Docker SDK or Terraform/Ansible to spin up VMs/containers, configure HELK/Velociraptor, and start attacks.

Scaffold the engine to read JSON, launch processes, and emit status.

It should communicate with the backend to update progress.

Range tools (HELK & Velociraptor):

Add services in docker-compose.yml for HELK and Velociraptor if containerized. (Alternatively, the scenario engine could deploy these as needed.)

Example: use the official HELK Dockerfiles or Cyb3rWard0g/HELK (the Docker version is in its docker/ folder). Use Velociraptor’s docker from Rapid7 or build from source.

Ensure network connectivity: Blue Team GUI connects to HELK (Kibana) and to Velociraptor UI/Server.

Document commands to initialize HELK (e.g. ingest logs) and register endpoints with Velociraptor.

AI Orchestrator:

In ai-orchestrator/, create a service (e.g. Python with Flask or Node with Express) that listens for tasks (via the backend or its own API).

Install the chosen AI SDK or CLI: e.g. @xai/claude-code npm package or OpenAI’s codex via CLI.

Scaffold an “agent loop”: input prompt → AI model → output.

Example code snippet in ai-orchestrator/src/orchestrator.js:

// Pseudocode using Node
const { Agent } = require('codex-cli');
async function runTask(prompt) {
  const agent = new Agent();
  const response = await agent.run(prompt);
  return response;
}

Expose an endpoint /ai/generate that takes JSON instructions and returns the AI’s output.

Docker Compose finalization:

Ensure each service’s Dockerfile builds correctly.

Complete docker-compose.yml with proper ports, depends_on, and volumes.

Example network: frontend (port 3000) ↔ backend (4000) ↔ db/lrs.

Test with docker-compose up --build to verify all containers start and connect (frontend should reach backend, backend should reach Postgres and LRS).

Throughout development, use Material UI components for all GUIs. For example, import MUI buttons, grids, and theming into the React apps. This ensures a consistent, responsive, and polished interface without reinventing UI elements.

Key References

cmi5/xAPI: cmi5 is an xAPI profile for LMS content launch, providing rules for launch and reporting. Using cmi5 allows us to track rich learning data (scores, completion) outside of SCORM. It also enables distributed content and easier integration. We will integrate an xAPI LRS (e.g. SQL LRS) to receive these statements.

Material UI: We implement Google’s Material Design through Material UI (MUI), an open-source React component library. This gives us beautiful default components and layouts, accelerating frontend development.

HELK & Velociraptor: HELK (Hunting ELK) is an open-source threat-hunting platform with advanced analytics on top of the ELK stack. Velociraptor is an advanced DFIR/endpoint monitoring tool. Both can run in Docker and will serve as the backbone of the blue team’s detection capabilities.

Docker Compose: For local development and simple orchestration, Docker Compose is preferred over Kubernetes. It’s “simple to use” and ideal for small-to-medium multi-service setups. We define all our components in docker-compose.yml so one command spins up the full environment.

This markdown spec should guide the Claude Codex/Opus agent to scaffold the project step-by-step: first initializing the backbone template, then creating each service directory with npx or boilerplate code, and configuring Dockerfiles and the Compose file. Each stage can be incremental, building from the architecture defined above.

Sources: This design references official docs and best practices for xAPI/cmi5, Material UI, HELK, Velociraptor, and Docker Compose vs Kubernetes guidance. These citations justify our architecture choices and tool selections.

# COTE Cyber Range Platform (“CanaryEh”) – Spec (en-CA)

**Executive Summary:** COTE (codename *CanaryEh*) is a planned Canadian on-premises Persistent Cyber Training Environment (PCTE) and LMS integration platform. It provides a multi-tenant cyber range with scenario authoring, attack/user emulation, telemetry, scoring, and AAR tools, inspired by SimSpace, Cyberbit, and DoD PCTE capabilities【20†L208-L214】【25†L139-L148】. COTE supports individual and team training, live-fire exercises, certification drills, and mission rehearsal, leveraging cloud-native orchestration and AI to create, run, and evolve realistic training labs on demand【25†L94-L102】【22†L53-L58】. Key features include a **range workbench** (drag/drop or YAML IaC editor)【6†L242-L248】, automated **attack scenario engine** (MITRE-mapped kill chains, real-world payloads)【51†L204-L209】, integration of real security tools (SIEM/EDR)【51†L228-L235】, and AI assistants for range generation and training aids【6†L279-L287】【22†L60-L63】. Users (students, instructors) interact via a web UI (Angular+Material M3), while backend services (NestJS microservices on Kubernetes) manage orchestration, telemetry ingestion, scoring, LRS, and AI. COTE emits cmi5/xAPI learning records for every training event (launch, completion, user actions) to an LRS, enabling integration with LMS and analytics. Security and isolation are paramount (tenant-based RBAC, sandboxing). The spec covers prioritized implementation stages, architecture diagrams (Mermaid), hardware sizing (4×Dell XE9780 B300 vs 4×XE9680 H200), full cmi5/xAPI statement profiles (with examples), LRS API contract, provider (K8s/Proxmox/VMware) interfaces, deployment/Helm templates, CI/CD/GitOps, AI (RAG) service design, UI component mapping, security checklist, test plan, and a prioritized backlog with effort estimates. 

## Features and Use Cases

- **Scenario Authoring & Workbench:** Drag-and-drop visual canvas plus YAML editor for defining networks, assets, user roles, and attack events. Supports versioning and IaC principles【6†L242-L248】【38†L400-L408】. 
- **Automated Attack Emulation:** Full-stack attack scenarios (e.g. SolarWinds, WannaCry) with complete kill-chain execution, mapped to MITRE ATT&CK and NIST NICE frameworks for training analytics【51†L204-L209】【51†L152-L155】. Generates dynamic threats and traffic across network, host, and application layers【51†L197-L204】. 
- **Traffic & Threat Injection:** Synthetic benign traffic and adversary tactics/emulation engines produce realistic network traffic and user behaviour (Endpoints, ICS/OT, cloud). Ranges may tie into real hardware or cloud resources【40†L455-L464】【51†L197-L204】. 
- **Realistic Environments:** Fully replicated enterprise-grade IT infrastructures: segmented subnets, domain controllers, mail/DNS servers, endpoints and tools (SIEMs, EDR, firewalls)【51†L228-L235】【25†L139-L147】. Virtualization (hypervisors, cloud, containers) for scalability and fidelity【40†L485-L494】【40†L500-L508】. 
- **Multi-User Training:** Supports live-fire exercises for red/blue/purple teams, tabletop, SOC drills, and mission rehearsals. Participants can join worldwide via web client【25†L94-L102】【22†L21-L30】. Instructors schedule exercises, assign students or teams (static or random team assignment), and track progress.
- **Adaptive Exercise Engine:** Through the Range Workbench and AI (e.g. ARIA-like natural language interface), instructors quickly generate or modify scenarios and environments (minutes vs days)【6†L279-L287】【25†L135-L143】. AI bots can script user behaviours or inject events (emails, alerts) to enhance realism【22†L60-L63】.
- **Tools Validation (“Tool Tester”):** Isolated testbed for validating security tool configurations and detection rules under live-fire. Allows tuning SIEM/EDR rules without impacting prod【6†L254-L261】.
- **Team Training & Tracking (“Team Trainer”):** Streamlined exercise planning and execution: schedule scenarios, manage war-room consoles, and repeatable runs. Configurable roles and missions reduce overhead【6†L265-L272】.
- **Telemetry Collection & Scoring:** Continuous log capture (network, host, applications) with ability to inject detection alerts. Builds timeline of trainee actions and generates performance metrics (detection rate, response time). Stores all events for AAR. 
- **After-Action Review (AAR):** Post-exercise report builder: replay timeline, show missed alerts or injected tasks, scorecards and leaderboards. Optionally includes quizzes or exams.
- **Content Catalog & Templates:** Library of prebuilt scenarios (attack playbooks, red-team drills) and customizable range templates. Shared across tenants (e.g. via classified catalog for government).
- **LMS Integration (cmi5/xAPI):** Every exercise and learning action (range launch, scenario complete, user tasks) generates xAPI statements under the cmi5 profile. Allows LRS-based tracking and integration with federated LMS or e-portfolio【51†L152-L155】【20†L208-L214】.
- **Federation & Multi-Domain Use:** Potential integration into allied exercises (e.g. DSO hackathons, coalition training). PCTE has been extended across SECRET/TS networks【25†L131-L139】【25†L150-L158】.
- **Administration:** Tenant/RBAC (org units, instructors, students), usage quotas, audit logs. System health dashboards.

The above reflects capabilities of enterprise/government cyber ranges【25†L139-L148】【38†L400-L408】. Key design principles: realism, fidelity, scalability, and strong capture of learning outcomes【40†L485-L494】【25†L139-L148】.

## System Architecture

COTE will use a microservices architecture on Kubernetes (or similar). Major components (see Mermaid diagram):

```mermaid
flowchart LR
    subgraph UI/Client
        Browser[Angular Web UI] 
    end
    subgraph API
        APIGateway["API Gateway (GraphQL/REST)"]
        AuthSvc["Auth Service (OIDC)"]
        UserSvc["User/Tenant Service"]
        RangeSvc["Range Orchestrator"]
        ScenarioSvc["Scenario Manager"]
        TelemetrySvc["Telemetry Ingest"]
        ScoringSvc["Scoring/AAR Service"]
        LRS["LRS (xAPI) Service"]
        AiSvc["AI Assistants (RAG LLMs)"]
    end
    subgraph Infra
        K8s["Kubernetes Cluster\n(Namespaces per Tenant)"]
        VMHost["Virtual HW (Proxmox/VMWare)"]
        DB["Databases: SQL for config, TSDB for logs, Vector DB"]
        FS["File/Obj Storage (scenarii, logs)"]
    end

    Browser -->|HTTPS/REST| APIGateway
    APIGateway --> AuthSvc
    AuthSvc --> UserSvc
    APIGateway --> UserSvc
    APIGateway --> RangeSvc
    APIGateway --> ScenarioSvc
    APIGateway --> TelemetrySvc
    APIGateway --> ScoringSvc
    APIGateway --> LRS
    APIGateway --> AiSvc

    RangeSvc -->|K8s APIs| K8s
    RangeSvc -->|Hypervisor APIs| VMHost
    ScenarioSvc --> RangeSvc
    TelemetrySvc --> DB & FS
    TelemetrySvc --> ScoringSvc
    ScoringSvc --> LRS
    AiSvc --> DB  -- Training data-->
    AiSvc --> LRS  -- In-context data-->
