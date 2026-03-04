# TrueNorth SDK

Python SDK for the TrueNorth Range platform.

## Installation

```bash
pip install truenorth-sdk
```

Or install from source:

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
from truenorth_sdk import TrueNorthClient

client = TrueNorthClient(base_url="http://localhost:8080", token="your-api-token")

# List ranges
ranges = client.ranges.list()

# Create a range
new_range = client.ranges.create(name="My Range", template_id="tmpl-001")

# Search telemetry
events = client.telemetry.search(range_id="rng-001", query="event_type:dns_query")
```

## Features

- **Range management** — create, list, destroy, snapshot ranges
- **Scenario management** — list and inspect scenarios
- **Exercise lifecycle** — start, complete, generate AARs
- **Template operations** — list, validate, publish templates
- **Telemetry search** — query OpenSearch-backed telemetry data
- **Async support** — async variants of all operations via `AsyncTrueNorthClient`

## Authentication

```python
# Option 1: Pass token directly
client = TrueNorthClient(token="your-token")

# Option 2: Login with credentials
client = TrueNorthClient()
client.login(username="admin", password="secret")

# Option 3: Environment variable
# Set TRUENORTH_API_TOKEN=your-token
client = TrueNorthClient()  # picks up token automatically
```

## Development

```bash
pip install -e ".[dev]"
pytest
```