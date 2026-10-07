# Scenario inject execution

How a timeline event becomes an inject, what the mock backend does, and where the
evidence goes. Code: `control-plane/worker/worker/inject_dispatch.py` (the seam),
`exercise_run.py` (the run loops), `scenario-engine/scenario_engine/injectors/` (the
registry), `control-plane/api/app/scenario_runs/` (the tables).

## Path of one inject

1. `run_scenario_v2` (exercise), `run_scenario_execution` (`POST /scenarios/execute`) or
   `run_inject` (instructor, `POST /ops/exercises/{id}/inject`) calls
   `dispatch_inject` / `dispatch_for_execution`.
2. The dispatch reads the range (`provisioner_output` -> VMs and networks,
   `provisioner_backend`) and builds a `RangeContext`.
3. `scenario_engine.injectors.run_inject(action, params, ctx)` runs the injector. It never
   raises. `inject.<name>` and `<name>` are the same action (authored scenarios use the
   prefix, the engine examples do not; `canonical_action` is the one adapter).
4. The injector's telemetry is sent to `ingest_telemetry_batch` (telemetry queue), which
   indexes it in `range-<range_id>`. Each event carries `exercise_id`, `inject_action`
   and `truenorth_simulated`.
5. The outcome is written to `inject_records` and pushed over `notify_api`.

Read it back: `GET /exercises/{id}/injects`, `GET /scenarios/executions/{id}/timeline`,
the exercise detail page's *Inject log*.

## Outcomes

| status    | meaning |
|-----------|---------|
| `fired`   | the injector ran and returned success |
| `skipped` | deliberately not run: it needs range hosts and the range has none (mock backend, or not provisioned), or an instructor inject arrived when the exercise was not running |
| `failed`  | unknown action, invalid params, injector exception, malformed timeline event, missing range, or the worker cannot import the scenario engine |

A failed or skipped inject never fails the run. Absence of an inject is never recorded as
success.

## The mock backend (decision, 2026-10-07)

Before: in mock mode `run_scenario_v2` skipped the seam entirely, slept, and marked every
objective achieved, so a "completed" mock exercise proved nothing about injection.

Now: every event goes through the dispatch on every backend. Each injector declares
`touches_range_hosts`:

- `False` (runs on mock): `simulated_execution`, `ad_attack`, `c2_beacon`,
  `file_operation`, `network_scan`. Their contract is to produce synthetic records only.
- `True` (recorded `skipped: mock backend (injector needs range hosts)`): `dns_spike`,
  `http_burst`, `email_phish`, `identity_new_admin_user`. Their contract is to act on a
  host (send traffic, deliver mail, create an account), even though today's code is
  synthetic too.

The default on the base class is `True`, so a new injector must opt in to running without
hosts. Separately, `execution_mode` says what the code actually does: every injector is
`"simulated"` today (no network, process or file I/O), and the records and telemetry say
so. A real injector must declare `"live"`.

The worker never scores (ADR 0005). On a real backend `run_scenario_v2` fires the timeline
and leaves the exercise `running` (notification phase `timeline_complete`): a Student
earns detection credit only by submitting a detection to the API, judged against the
range's telemetry inside the exercise window, until an instructor completes the exercise
or its duration runs out (`app/exercise_completion.py`). An inject's own telemetry
therefore credits nobody. On the mock backend `run_scenario_v2` still marks the exercise's
objectives achieved without evidence and completes it: a demo convenience, flagged here,
not an assessment. Scenario executions never score: their objectives are `unassessed`.

## Run-state rules

- `run_scenario_v2` starts only a `pending`/`running` exercise; a late or duplicate
  delivery to a paused, completed or cancelled exercise returns `skipped` and writes
  nothing.
- Before each event it re-reads the state; if an instructor paused, completed or cancelled
  the exercise it stops firing (`halted`). There is no resume: a paused exercise's
  remaining events are not fired by this run.
- It completes only a `running` exercise and cancels (after the last retry) only a
  `pending`/`running` one, so it cannot overwrite a newer terminal state.
- `run_id` is the Celery task id, stable across retries: a retry skips the events its run
  already recorded. A replay (`POST /exercises/{id}/run`) is a new run; earlier records
  are kept.

## Deployment

The worker image copies the `scenario_engine` package in (Dockerfile,
`--build-context scenario_engine=scenario-engine`; compose `additional_contexts`), as
detection scoring already requires, and sets `PYTHONPATH=/app`: `celery -A` keeps the cwd
on `sys.path` only while importing the app, so without it the engine was in the image but
not importable in the running worker (every inject failed; detection scoring silently
stayed off). A worker built without the engine records every inject
`failed: scenario-engine not importable in this worker`.
`tests/contracts/test_worker_image_contexts.py` checks every compose and CI build of the
worker supplies the named context and that the Dockerfile keeps `/app` on the path.

The dispatch does not read `OPENSEARCH_URL` (MOSA: only the ingest task does); no
injector reads `RangeContext.opensearch_url`, so it keeps the engine default.
