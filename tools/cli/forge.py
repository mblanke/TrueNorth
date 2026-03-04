#!/usr/bin/env python3
"""TrueNorth Range CLI — manage ranges, scenarios, exercises from the command line.

Usage:
    python -m tools.cli.forge --help
    forge range list
    forge range create --name "My Range" --template-id <uuid>
    forge scenario list
    forge exercise start <exercise_id>
    forge telemetry search <range_id> --query "event_type:dns_query"
"""
from __future__ import annotations

import json
import os
import sys

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:
    print("Install CLI deps: pip install typer[all] rich httpx")
    sys.exit(1)

import httpx

API_URL = os.getenv("TRUENORTH_API_URL", "http://localhost:8080")
console = Console()
app = typer.Typer(name="forge", help="TrueNorth Range CLI")

# ── Sub-commands ───────────────────────────────────────────────────────
range_app = typer.Typer(help="Range management")
scenario_app = typer.Typer(help="Scenario management")
template_app = typer.Typer(help="Template management")
exercise_app = typer.Typer(help="Exercise management")
telemetry_app = typer.Typer(help="Telemetry queries")

app.add_typer(range_app, name="range")
app.add_typer(scenario_app, name="scenario")
app.add_typer(template_app, name="template")
app.add_typer(exercise_app, name="exercise")
app.add_typer(telemetry_app, name="telemetry")


def _headers() -> dict:
    """Return auth headers if TRUENORTH_API_TOKEN is set or token file exists."""
    token = os.getenv("TRUENORTH_API_TOKEN", "")
    if not token:
        token_path = os.path.expanduser("~/.truenorth/token")
        if os.path.exists(token_path):
            with open(token_path) as f:
                token = f.read().strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _get(path: str) -> dict | list:
    resp = httpx.get(f"{API_URL}{path}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def _post(path: str, data: dict) -> dict:
    resp = httpx.post(f"{API_URL}{path}", json=data, headers=_headers())
    resp.raise_for_status()
    return resp.json()


# ── Auth / Config ──────────────────────────────────────────────────────
@app.command("login")
def login(
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(..., prompt=True, hide_input=True),
):
    """Authenticate and store API token."""
    try:
        resp = httpx.post(f"{API_URL}/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        token = resp.json().get("access_token", "")
        if not token:
            console.print("[red]No access_token in response[/red]")
            raise typer.Exit(1)
        # Persist token to ~/.truenorth/token
        token_dir = os.path.expanduser("~/.truenorth")
        os.makedirs(token_dir, exist_ok=True)
        token_path = os.path.join(token_dir, "token")
        with open(token_path, "w") as f:
            f.write(token)
        os.environ["TRUENORTH_API_TOKEN"] = token
        console.print(f"[green]Logged in successfully.[/green] Token saved to {token_path}")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]Login failed:[/red] {exc.response.status_code} {exc.response.text}")
        raise typer.Exit(1)


@app.command("config")
def config(
    api_url: str = typer.Option(None, "--api-url", help="Set API base URL"),
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
):
    """Set or show CLI configuration."""
    global API_URL
    config_dir = os.path.expanduser("~/.truenorth")
    os.makedirs(config_dir, exist_ok=True)

    if api_url:
        API_URL = api_url
        os.environ["TRUENORTH_API_URL"] = api_url
        with open(os.path.join(config_dir, "api_url"), "w") as f:
            f.write(api_url)
        console.print(f"[green]API URL set to:[/green] {api_url}")

    if show or not api_url:
        token_path = os.path.join(config_dir, "token")
        token_exists = os.path.exists(token_path)
        console.print(f"API URL : {API_URL}")
        console.print(f"Token   : {'set (' + token_path + ')' if token_exists else 'not set'}")
        console.print(f"Env token: {'set' if os.getenv('TRUENORTH_API_TOKEN') else 'not set'}")


# ── Range ──────────────────────────────────────────────────────────────
@range_app.command("list")
def range_list():
    """List all ranges."""
    ranges = _get("/ranges")
    table = Table(title="Ranges")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Created")
    for r in ranges:
        table.add_row(r["id"], r["name"], r["state"], r["created_at"])
    console.print(table)


@range_app.command("create")
def range_create(name: str, template_id: str):
    """Create a new range."""
    result = _post("/ranges", {"name": name, "template_id": template_id})
    console.print(f"[green]Range created:[/green] {result['id']} (state: {result['state']})")


@range_app.command("destroy")
def range_destroy(range_id: str):
    """Destroy a range."""
    result = _post(f"/ranges/{range_id}/destroy", {})
    console.print(f"[yellow]Range destroying:[/yellow] {result['id']} (state: {result['state']})")


@range_app.command("get")
def range_get(range_id: str):
    """Get range details."""
    r = _get(f"/ranges/{range_id}")
    console.print_json(json.dumps(r, indent=2))




@range_app.command("batch-provision")
def range_batch_provision(range_ids: list[str]):
    """Provision multiple ranges in a batch (up to 500)."""
    result = _post("/ranges/batch-provision", {"range_ids": range_ids})
    console.print(f"[green]Batch provision started:[/green] task_id={result['task_id']}, {result['accepted']} ranges accepted")


@range_app.command("stats")
def range_stats(tenant_id: str = None):
    """Get aggregated range stats by state."""
    path = "/ranges/stats"
    if tenant_id:
        path += f"?tenant_id={tenant_id}"
    result = _get(path)
    table = Table(title="Range Stats")
    table.add_column("State")
    table.add_column("Count", justify="right")
    for state, count in result.get("by_state", {}).items():
        table.add_row(state, str(count))
    table.add_row("[bold]Total[/bold]", f"[bold]{result.get('total', 0)}[/bold]")
    console.print(table)

# ── Template ───────────────────────────────────────────────────────────
@template_app.command("list")
def template_list():
    """List all templates."""
    templates = _get("/templates")
    table = Table(title="Templates")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Public")
    for t in templates:
        table.add_row(t["id"], t["name"], t["version"], str(t.get("is_public", False)))
    console.print(table)


@template_app.command("validate")
def template_validate(path: str):
    """Validate a template YAML against the schema."""
    import yaml
    from jsonschema import validate, ValidationError
    from pathlib import Path

    schema = json.loads(Path("scenario-engine/schemas/template.schema.json").read_text())
    data = yaml.safe_load(Path(path).read_text())
    try:
        validate(data, schema)
        console.print(f"[green]Valid:[/green] {path}")
    except ValidationError as e:
        console.print(f"[red]Invalid:[/red] {e.message}")
        raise typer.Exit(1)


# ── Scenario ───────────────────────────────────────────────────────────
@scenario_app.command("list")
def scenario_list():
    """List all scenarios."""
    scenarios = _get("/scenarios")
    table = Table(title="Scenarios")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    for s in scenarios:
        table.add_row(s["id"], s["name"], s["version"])
    console.print(table)


# ── Exercise ───────────────────────────────────────────────────────────
@exercise_app.command("list")
def exercise_list():
    """List all exercises."""
    exercises = _get("/exercises")
    table = Table(title="Exercises")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Score")
    for e in exercises:
        table.add_row(e["id"], e["name"], e["state"], f"{e['total_score']}/{e['max_score']}")
    console.print(table)


@exercise_app.command("start")
def exercise_start(exercise_id: str):
    """Start an exercise."""
    result = _post(f"/exercises/{exercise_id}/start", {})
    console.print(f"[green]Exercise started:[/green] {result['id']}")


@exercise_app.command("complete")
def exercise_complete(exercise_id: str):
    """Complete an exercise."""
    result = _post(f"/exercises/{exercise_id}/complete", {})
    console.print(f"[green]Exercise completed:[/green] Score {result['total_score']}/{result['max_score']}")


@exercise_app.command("aar")
def exercise_aar(exercise_id: str):
    """Generate and display AAR."""
    _post(f"/exercises/{exercise_id}/aar/generate", {})
    aar = _get(f"/exercises/{exercise_id}/aar")
    console.print_json(aar["report_json"])


# ── Telemetry ──────────────────────────────────────────────────────────
@telemetry_app.command("search")
def telemetry_search(range_id: str, query: str = "*", size: int = 20):
    """Search telemetry events for a range."""
    result = _get(f"/telemetry/{range_id}/search?q={query}&size={size}")
    hits = result.get("hits", {}).get("hits", [])
    for hit in hits:
        console.print_json(json.dumps(hit.get("_source", {})))


# ── Health ─────────────────────────────────────────────────────────────
@app.command("health")
def health():
    """Check API health."""
    result = _get("/health")
    console.print(f"[green]{result['app']}[/green] v{result['version']} — {result['status']}")


if __name__ == "__main__":
    app()
