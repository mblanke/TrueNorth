#!/usr/bin/env python3
"""
TrueNorth Range - Database Seed Data Script

Populates the TrueNorth Range API with seed tenants, templates, scenarios,
users, ranges, and exercises. Idempotent: checks for existing data before
creating new records.

Usage:
    python seed_data.py [--api-url http://localhost:8080] [--dry-run]

Requirements:
    pip install httpx rich
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Install with: pip install httpx")
    sys.exit(1)

try:
    from rich import print as rprint
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
except ImportError:
    print("ERROR: rich is required. Install with: pip install rich")
    sys.exit(1)

console = Console()

# ---------------------------------------------------------------------------
# Seed Data Definitions
# ---------------------------------------------------------------------------

TENANTS = [
    {
        "name": "ACME Corp",
        "slug": "acme-corp",
        "description": "Global manufacturing and technology conglomerate",
        "contact_email": "admin@acme-corp.example.com",
        "max_concurrent_ranges": 10,
        "tier": "enterprise",
    },
    {
        "name": "Wayne Enterprises",
        "slug": "wayne-enterprises",
        "description": "Multinational conglomerate specializing in defense and technology",
        "contact_email": "admin@wayne-ent.example.com",
        "max_concurrent_ranges": 15,
        "tier": "enterprise",
    },
    {
        "name": "Stark Industries",
        "slug": "stark-industries",
        "description": "Advanced technology and clean energy research corporation",
        "contact_email": "admin@stark-ind.example.com",
        "max_concurrent_ranges": 20,
        "tier": "premium",
    },
]

TEMPLATES = [
    {
        "name": "Small Enterprise",
        "slug": "small-enterprise",
        "description": "Basic enterprise network with jump host, DC, and workstations",
        "vm_count": 5,
        "difficulty": "beginner",
        "source_file": "content/ranges/small-enterprise/template.yaml",
    },
    {
        "name": "Medium Enterprise",
        "slug": "medium-enterprise",
        "description": "Medium enterprise with DMZ, servers, and corporate segments",
        "vm_count": 8,
        "difficulty": "intermediate",
        "source_file": "content/ranges/medium-enterprise/template.yaml",
    },
    {
        "name": "Large Enterprise",
        "slug": "large-enterprise",
        "description": "Full enterprise with 3 AD forests, DMZ, OT/SCADA, and cloud hybrid",
        "vm_count": 54,
        "difficulty": "advanced",
        "source_file": "content/ranges/large-enterprise/template.yaml",
    },
    {
        "name": "SOC Training",
        "slug": "soc-training",
        "description": "SOC analyst training with attacker infra, victim network, and full SOC toolset",
        "vm_count": 25,
        "difficulty": "intermediate",
        "source_file": "content/ranges/soc-training/template.yaml",
    },
    {
        "name": "Red Team Operations",
        "slug": "red-team",
        "description": "Red team environment with target org, attacker infra, and pivot networks",
        "vm_count": 30,
        "difficulty": "advanced",
        "source_file": "content/ranges/red-team/template.yaml",
    },
    {
        "name": "Cloud Security",
        "slug": "cloud-security",
        "description": "Cloud security training with simulated AWS/Azure, k3s, and CI/CD",
        "vm_count": 20,
        "difficulty": "intermediate",
        "source_file": "content/ranges/cloud-security/template.yaml",
    },
]

SCENARIOS = [
    {
        "name": "Ransomware Lite",
        "slug": "ransomware-lite",
        "description": "Basic ransomware attack chain from phishing to encryption",
        "difficulty": "beginner",
        "duration_minutes": 30,
        "template_slug": "small-enterprise",
        "mitre_techniques": ["T1204", "T1486", "T1490"],
        "source_file": "content/scenarios/ransomware-lite/scenario.yaml",
    },
    {
        "name": "APT Nation-State",
        "slug": "apt-nation-state",
        "description": "Advanced persistent threat: full kill chain from spearphishing to exfiltration",
        "difficulty": "hard",
        "duration_minutes": 90,
        "template_slug": "large-enterprise",
        "mitre_techniques": ["T1566.001", "T1059.005", "T1053.005", "T1570", "T1560.001", "T1048.002"],
        "source_file": "content/scenarios/apt-nation-state/scenario.yaml",
    },
    {
        "name": "Insider Threat Advanced",
        "slug": "insider-threat-advanced",
        "description": "Disgruntled employee data exfiltration with anti-forensics",
        "difficulty": "medium",
        "duration_minutes": 60,
        "template_slug": "medium-enterprise",
        "mitre_techniques": ["T1078", "T1039", "T1052.001", "T1567.002", "T1070.001"],
        "source_file": "content/scenarios/insider-threat-advanced/scenario.yaml",
    },
    {
        "name": "Cloud Breach",
        "slug": "cloud-breach",
        "description": "Cloud infrastructure compromise via exposed S3, IAM escalation, Lambda backdoor",
        "difficulty": "hard",
        "duration_minutes": 75,
        "template_slug": "cloud-security",
        "mitre_techniques": ["T1530", "T1078.004", "T1098.003", "T1584.007", "T1562.008"],
        "source_file": "content/scenarios/cloud-breach/scenario.yaml",
    },
    {
        "name": "ICS/SCADA Attack",
        "slug": "ics-attack",
        "description": "Industrial control system attack targeting PLC logic and safety systems",
        "difficulty": "expert",
        "duration_minutes": 60,
        "template_slug": "large-enterprise",
        "mitre_techniques": ["T0886", "T0843", "T0821", "T0880", "T0831"],
        "source_file": "content/scenarios/ics-attack/scenario.yaml",
    },
    {
        "name": "Incident Response Drill",
        "slug": "incident-response-drill",
        "description": "Blue team IR exercise with pre-staged compromised environment",
        "difficulty": "medium",
        "duration_minutes": 60,
        "template_slug": "soc-training",
        "mitre_techniques": ["T1566.001", "T1059.001", "T1053.005", "T1003.001", "T1486"],
        "source_file": "content/scenarios/incident-response-drill/scenario.yaml",
    },
]

USERS_PER_TENANT = [
    {"username": "admin", "role": "admin", "display_name": "Tenant Administrator"},
    {"username": "instructor", "role": "instructor", "display_name": "Lead Instructor"},
    {"username": "trainee1", "role": "trainee", "display_name": "Trainee One"},
    {"username": "trainee2", "role": "trainee", "display_name": "Trainee Two"},
    {"username": "trainee3", "role": "trainee", "display_name": "Trainee Three"},
    {"username": "trainee4", "role": "trainee", "display_name": "Trainee Four"},
    {"username": "trainee5", "role": "trainee", "display_name": "Trainee Five"},
]

SAMPLE_RANGES = [
    {
        "name": "ACME SOC Training Lab",
        "tenant_slug": "acme-corp",
        "template_slug": "soc-training",
        "status": "provisioned",
    },
    {
        "name": "ACME Ransomware Exercise",
        "tenant_slug": "acme-corp",
        "template_slug": "small-enterprise",
        "status": "provisioned",
    },
    {
        "name": "Wayne Red Team Range",
        "tenant_slug": "wayne-enterprises",
        "template_slug": "red-team",
        "status": "provisioned",
    },
    {
        "name": "Wayne Cloud Security Lab",
        "tenant_slug": "wayne-enterprises",
        "template_slug": "cloud-security",
        "status": "provisioned",
    },
    {
        "name": "Stark Enterprise Range",
        "tenant_slug": "stark-industries",
        "template_slug": "large-enterprise",
        "status": "provisioned",
    },
    {
        "name": "Stark ICS/SCADA Lab",
        "tenant_slug": "stark-industries",
        "template_slug": "large-enterprise",
        "status": "provisioned",
    },
]

SAMPLE_EXERCISES = [
    {
        "name": "ACME Q1 SOC Training - Ransomware",
        "tenant_slug": "acme-corp",
        "scenario_slug": "ransomware-lite",
        "range_name": "ACME Ransomware Exercise",
        "status": "scheduled",
        "scheduled_start": "2026-03-15T09:00:00Z",
        "max_participants": 10,
    },
    {
        "name": "ACME IR Drill - March 2026",
        "tenant_slug": "acme-corp",
        "scenario_slug": "incident-response-drill",
        "range_name": "ACME SOC Training Lab",
        "status": "scheduled",
        "scheduled_start": "2026-03-20T14:00:00Z",
        "max_participants": 8,
    },
    {
        "name": "Wayne Red Team Assessment",
        "tenant_slug": "wayne-enterprises",
        "scenario_slug": "apt-nation-state",
        "range_name": "Wayne Red Team Range",
        "status": "scheduled",
        "scheduled_start": "2026-04-01T10:00:00Z",
        "max_participants": 6,
    },
    {
        "name": "Wayne Cloud Security Workshop",
        "tenant_slug": "wayne-enterprises",
        "scenario_slug": "cloud-breach",
        "range_name": "Wayne Cloud Security Lab",
        "status": "draft",
        "max_participants": 12,
    },
    {
        "name": "Stark ICS Security Exercise",
        "tenant_slug": "stark-industries",
        "scenario_slug": "ics-attack",
        "range_name": "Stark ICS/SCADA Lab",
        "status": "scheduled",
        "scheduled_start": "2026-03-25T08:00:00Z",
        "max_participants": 5,
    },
    {
        "name": "Stark Insider Threat Tabletop",
        "tenant_slug": "stark-industries",
        "scenario_slug": "insider-threat-advanced",
        "range_name": "Stark Enterprise Range",
        "status": "draft",
        "max_participants": 15,
    },
]


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

@dataclass
class SeedClient:
    """HTTP client wrapper for the TrueNorth Range API."""

    base_url: str
    dry_run: bool = False
    client: httpx.Client = field(default=None, init=False, repr=False)

    # Caches for ID lookups
    _tenant_ids: dict[str, str] = field(default_factory=dict, init=False)
    _template_ids: dict[str, str] = field(default_factory=dict, init=False)
    _scenario_ids: dict[str, str] = field(default_factory=dict, init=False)
    _range_ids: dict[str, str] = field(default_factory=dict, init=False)

    # Counters
    created: int = field(default=0, init=False)
    skipped: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    # ------ Helpers ------

    def _get(self, path: str) -> httpx.Response | None:
        """GET with error handling."""
        try:
            resp = self.client.get(path)
            return resp
        except httpx.HTTPError as exc:
            console.print(f"  [red]HTTP error on GET {path}: {exc}[/red]")
            self.errors += 1
            return None

    def _post(self, path: str, json: dict[str, Any]) -> httpx.Response | None:
        """POST with error handling."""
        if self.dry_run:
            console.print(f"  [dim](dry-run) POST {path}[/dim]")
            self.created += 1
            return None
        try:
            resp = self.client.post(path, json=json)
            if resp.status_code in (200, 201):
                self.created += 1
                return resp
            elif resp.status_code == 409:
                self.skipped += 1
                return resp
            else:
                console.print(
                    f"  [red]POST {path} returned {resp.status_code}: "
                    f"{resp.text[:200]}[/red]"
                )
                self.errors += 1
                return resp
        except httpx.HTTPError as exc:
            console.print(f"  [red]HTTP error on POST {path}: {exc}[/red]")
            self.errors += 1
            return None

    def _exists(self, path: str, name_field: str, name_value: str) -> str | None:
        """Check if a resource already exists. Returns its ID or None."""
        resp = self._get(path)
        if resp is None or resp.status_code != 200:
            return None
        try:
            items = resp.json()
            if isinstance(items, dict) and "items" in items:
                items = items["items"]
            if isinstance(items, list):
                for item in items:
                    if item.get(name_field) == name_value or item.get("slug") == name_value:
                        return item.get("id") or item.get("slug")
        except Exception:
            pass
        return None

    # ------ Seed Methods ------

    def seed_tenants(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Tenants[/bold cyan]")
        for tenant in TENANTS:
            existing_id = self._exists("/api/v1/tenants", "slug", tenant["slug"])
            if existing_id:
                console.print(f"  [yellow]SKIP[/yellow] Tenant '{tenant['name']}' already exists (id={existing_id})")
                self._tenant_ids[tenant["slug"]] = existing_id
                self.skipped += 1
                continue
            resp = self._post("/api/v1/tenants", tenant)
            if resp and resp.status_code in (200, 201):
                data = resp.json()
                tid = data.get("id", data.get("slug", tenant["slug"]))
                self._tenant_ids[tenant["slug"]] = tid
                console.print(f"  [green]CREATED[/green] Tenant '{tenant['name']}' (id={tid})")
            elif self.dry_run:
                self._tenant_ids[tenant["slug"]] = f"dry-{tenant['slug']}"
                console.print(f"  [green]CREATED[/green] Tenant '{tenant['name']}' (dry-run)")

    def seed_templates(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Range Templates[/bold cyan]")
        for tpl in TEMPLATES:
            existing_id = self._exists("/api/v1/templates", "slug", tpl["slug"])
            if existing_id:
                console.print(f"  [yellow]SKIP[/yellow] Template '{tpl['name']}' already exists")
                self._template_ids[tpl["slug"]] = existing_id
                self.skipped += 1
                continue
            resp = self._post("/api/v1/templates", tpl)
            if resp and resp.status_code in (200, 201):
                data = resp.json()
                tid = data.get("id", data.get("slug", tpl["slug"]))
                self._template_ids[tpl["slug"]] = tid
                console.print(f"  [green]CREATED[/green] Template '{tpl['name']}' ({tpl['vm_count']} VMs)")
            elif self.dry_run:
                self._template_ids[tpl["slug"]] = f"dry-{tpl['slug']}"
                console.print(f"  [green]CREATED[/green] Template '{tpl['name']}' (dry-run)")

    def seed_scenarios(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Scenarios[/bold cyan]")
        for scn in SCENARIOS:
            existing_id = self._exists("/api/v1/scenarios", "slug", scn["slug"])
            if existing_id:
                console.print(f"  [yellow]SKIP[/yellow] Scenario '{scn['name']}' already exists")
                self._scenario_ids[scn["slug"]] = existing_id
                self.skipped += 1
                continue
            payload = {**scn}
            payload["template_id"] = self._template_ids.get(scn.pop("template_slug", ""), "")
            resp = self._post("/api/v1/scenarios", payload)
            if resp and resp.status_code in (200, 201):
                data = resp.json()
                sid = data.get("id", data.get("slug", scn["slug"]))
                self._scenario_ids[scn["slug"]] = sid
                console.print(
                    f"  [green]CREATED[/green] Scenario '{scn['name']}' "
                    f"(difficulty={scn['difficulty']}, {scn['duration_minutes']}min)"
                )
            elif self.dry_run:
                self._scenario_ids[scn["slug"]] = f"dry-{scn['slug']}"
                console.print(f"  [green]CREATED[/green] Scenario '{scn['name']}' (dry-run)")

    def seed_users(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Users[/bold cyan]")
        for tenant in TENANTS:
            tenant_slug = tenant["slug"]
            tenant_id = self._tenant_ids.get(tenant_slug, tenant_slug)
            console.print(f"  [bold]Tenant: {tenant['name']}[/bold]")
            for user in USERS_PER_TENANT:
                qualified_name = f"{user['username']}@{tenant_slug}"
                existing = self._exists(
                    f"/api/v1/tenants/{tenant_id}/users",
                    "username",
                    user["username"],
                )
                if existing:
                    console.print(f"    [yellow]SKIP[/yellow] User '{qualified_name}' exists")
                    self.skipped += 1
                    continue
                payload = {
                    **user,
                    "email": f"{user['username']}@{tenant_slug}.example.com",
                    "tenant_id": tenant_id,
                }
                resp = self._post(f"/api/v1/tenants/{tenant_id}/users", payload)
                if resp and resp.status_code in (200, 201):
                    console.print(f"    [green]CREATED[/green] User '{qualified_name}' (role={user['role']})")
                elif self.dry_run:
                    console.print(f"    [green]CREATED[/green] User '{qualified_name}' (dry-run)")

    def seed_ranges(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Sample Ranges[/bold cyan]")
        for rng in SAMPLE_RANGES:
            tenant_id = self._tenant_ids.get(rng["tenant_slug"], rng["tenant_slug"])
            template_id = self._template_ids.get(rng["template_slug"], rng["template_slug"])
            existing = self._exists(
                f"/api/v1/tenants/{tenant_id}/ranges",
                "name",
                rng["name"],
            )
            if existing:
                console.print(f"  [yellow]SKIP[/yellow] Range '{rng['name']}' exists")
                self._range_ids[rng["name"]] = existing
                self.skipped += 1
                continue
            payload = {
                "name": rng["name"],
                "tenant_id": tenant_id,
                "template_id": template_id,
                "status": rng["status"],
            }
            resp = self._post(f"/api/v1/tenants/{tenant_id}/ranges", payload)
            if resp and resp.status_code in (200, 201):
                data = resp.json()
                rid = data.get("id", rng["name"])
                self._range_ids[rng["name"]] = rid
                console.print(
                    f"  [green]CREATED[/green] Range '{rng['name']}' "
                    f"(tenant={rng['tenant_slug']}, template={rng['template_slug']})"
                )
            elif self.dry_run:
                self._range_ids[rng["name"]] = f"dry-{rng['name']}"
                console.print(f"  [green]CREATED[/green] Range '{rng['name']}' (dry-run)")

    def seed_exercises(self) -> None:
        console.print("\n[bold cyan]>>> Seeding Sample Exercises[/bold cyan]")
        for ex in SAMPLE_EXERCISES:
            tenant_id = self._tenant_ids.get(ex["tenant_slug"], ex["tenant_slug"])
            scenario_id = self._scenario_ids.get(ex["scenario_slug"], ex["scenario_slug"])
            range_id = self._range_ids.get(ex["range_name"], ex["range_name"])
            existing = self._exists(
                f"/api/v1/tenants/{tenant_id}/exercises",
                "name",
                ex["name"],
            )
            if existing:
                console.print(f"  [yellow]SKIP[/yellow] Exercise '{ex['name']}' exists")
                self.skipped += 1
                continue
            payload = {
                "name": ex["name"],
                "tenant_id": tenant_id,
                "scenario_id": scenario_id,
                "range_id": range_id,
                "status": ex["status"],
                "max_participants": ex["max_participants"],
            }
            if "scheduled_start" in ex:
                payload["scheduled_start"] = ex["scheduled_start"]
            resp = self._post(f"/api/v1/tenants/{tenant_id}/exercises", payload)
            if resp and resp.status_code in (200, 201):
                console.print(
                    f"  [green]CREATED[/green] Exercise '{ex['name']}' "
                    f"(scenario={ex['scenario_slug']}, status={ex['status']})"
                )
            elif self.dry_run:
                console.print(f"  [green]CREATED[/green] Exercise '{ex['name']}' (dry-run)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_summary(client: SeedClient) -> None:
    """Print a summary table of seeding results."""
    table = Table(title="Seed Data Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Created", f"[green]{client.created}[/green]")
    table.add_row("Skipped (already exist)", f"[yellow]{client.skipped}[/yellow]")
    table.add_row("Errors", f"[red]{client.errors}[/red]")
    table.add_row("Total Tenants", str(len(TENANTS)))
    table.add_row("Total Templates", str(len(TEMPLATES)))
    table.add_row("Total Scenarios", str(len(SCENARIOS)))
    table.add_row("Total Users", str(len(TENANTS) * len(USERS_PER_TENANT)))
    table.add_row("Total Ranges", str(len(SAMPLE_RANGES)))
    table.add_row("Total Exercises", str(len(SAMPLE_EXERCISES)))
    console.print()
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the TrueNorth Range API with sample data"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8080",
        help="Base URL of the TrueNorth Range API (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making API calls",
    )
    args = parser.parse_args()

    console.print("[bold]TrueNorth Range - Seed Data Script[/bold]")
    console.print(f"API URL: {args.api_url}")
    console.print(f"Dry Run: {args.dry_run}")
    console.print("=" * 60)

    # Health check
    if not args.dry_run:
        try:
            resp = httpx.get(f"{args.api_url}/health", timeout=5.0)
            if resp.status_code != 200:
                console.print(
                    f"[red]API health check failed (status={resp.status_code}). "
                    f"Is the server running at {args.api_url}?[/red]"
                )
                sys.exit(1)
            console.print("[green]API health check passed[/green]")
        except httpx.HTTPError as exc:
            console.print(
                f"[red]Cannot reach API at {args.api_url}: {exc}[/red]\n"
                f"[dim]Start the API server or use --dry-run to preview.[/dim]"
            )
            sys.exit(1)

    client = SeedClient(base_url=args.api_url, dry_run=args.dry_run)
    start_time = time.monotonic()

    try:
        client.seed_tenants()
        client.seed_templates()
        client.seed_scenarios()
        client.seed_users()
        client.seed_ranges()
        client.seed_exercises()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
    finally:
        elapsed = time.monotonic() - start_time
        print_summary(client)
        console.print(f"\n[dim]Completed in {elapsed:.1f}s[/dim]")
        client.close()

    if client.errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
