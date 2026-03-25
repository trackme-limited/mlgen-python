# ML Gen — Developer Guide

ML Gen is a synthetic metrics generator for testing and qualifying TrackMe's Machine Learning Outlier Detection. It produces realistic time-series data (`dcount_hosts`, `events_count`) for multiple entities with configurable seasonality, anomaly cycling, and HEC delivery.

## Repository Structure

```
mlgen-python/
├── ml_gen.py              # Main generator — all logic in a single file
├── version.json           # App version (semver: major.minor.patch)
├── Dockerfile             # Python 3.13-slim container, non-root user
├── docker-compose.yml     # Single service with .env file config
├── entrypoint.sh          # Validates HEC token, prints config banner, exec python3
├── .env.example           # All configuration options documented with comments
├── requirements.txt       # Python deps: requests, urllib3
├── README.md              # User-facing documentation
└── CLAUDE.md              # This file — developer architecture reference
```

## Versioning

Version is stored in `version.json` at the repo root:
```json
{"version": "1.1.0", "app_id": "mlgen-python", "description": "..."}
```
`ml_gen.py` reads `__version__` from this file at import time.

## CI/CD & Branching

- **`main`**: production branch
- **`version_XXYY`**: version branches for releases (e.g., `version_1100` for v1.1.0)
- Feature/fix branches are merged into version branches, then version branches into main

## Architecture Overview

### Execution Flow

```
Container starts
  → entrypoint.sh (validates HEC token, prints config banner)
    → ml_gen.py main()
      1. load_config() — reads all env vars
      2. instance_id — from INSTANCE_ID env or generate UUID
      3. build_entity_profiles() — creates entity list from catalogs
      4. DISABLE_ALL_ANOMALIES check — overrides all behaviors to normal if set
      5. run_backfill() — generates BACKFILL_DAYS of historical data (all normal)
         ↳ SKIPPED if INSTANCE_ID is set (data already exists in Splunk)
      6. run_continuous() — infinite loop generating real-time data with anomaly cycling
```

### Key Classes

#### `EntityProfile`
Represents a single entity with its own baseline, behavior, and anomaly cycling state.

| Field | Type | Description |
|-------|------|-------------|
| `ref` | `str` | Entity identifier (e.g., `security:linux_secure`) |
| `behavior` | `str` | `"normal"`, `"lower_outlier"`, or `"upper_outlier"` |
| `baseline` | `dict` | `{"dcount_hosts": (lo, hi), "events_count": (lo, hi)}` |
| `scale_factor` | `float` | Per-entity multiplier (0.8–1.2) for uniqueness |
| `flat` | `bool` | `True` = no seasonality (IT ops metrics) |
| `short_cycle_hours` | `int \| None` | If set, fixed anomaly duration; normal = 24 - this |
| `is_in_anomaly` | `bool` | Current anomaly cycling state |
| `phase_end_time` | `datetime \| None` | When current phase (anomaly/normal) expires |

Key methods:
- `is_outlier()` — whether behavior is lower/upper outlier
- `get_effective_behavior()` — returns current behavior accounting for cycling
- `maybe_transition()` — checks if phase expired and transitions anomaly↔normal
- `_get_anomaly_duration()` / `_get_normal_duration()` — returns durations (short-cycle-aware)

#### `HECSender`
Batched HEC event sender with retry and exponential backoff.

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_RETRIES` | 5 | Attempts before dropping events |
| `INITIAL_BACKOFF` | 2.0s | First retry delay |
| `BACKOFF_MULTIPLIER` | 2.0 | Exponential growth factor |
| `MAX_BACKOFF` | 60.0s | Cap on retry delay |

Methods:
- `enqueue(metric)` — adds to queue, auto-flushes at batch size
- `flush()` — sends all queued events to HEC
- `_send_with_retry(payload, event_count)` — retry loop with backoff

### Entity Catalogs

Three entity sources, defined as module-level constants:

1. **`ENTITY_CATALOG`** (20 entries) — Seasonal entities with realistic Splunk data source names (e.g., `security:linux_secure`, `cloud:aws:cloudtrail`). Wide baseline ranges for distinct profiles.

2. **`FLAT_ENTITY_CATALOG`** (8 entries) — IT ops / metrics collection entities with narrow baseline ranges (±5-10% variation). No day/hour seasonality. Names like `metrics:collectd`, `itops:snmp:traps`.

3. **`SHORT_CYCLE_ENTITIES`** (3 entries) — Hardcoded entities pulled from `ENTITY_CATALOG` by exact `ref` match. Each has a fixed `anomaly_hours` duration with normal = 24 - anomaly hours, creating daily cycles for demos.

### Entity Profile Building (`build_entity_profiles()`)

Order of entity creation:
1. **Short-cycle entities** — matched by exact ref from `ENTITY_CATALOG`, removed from the seasonal pool to avoid duplicates
2. **Seasonal entities** — picked from remaining `ENTITY_CATALOG` (shuffled with seed 42), split across normal/lower/upper by `NUM_*` counts
3. **Flat entities** — picked from `FLAT_ENTITY_CATALOG` (shuffled with seed 42), split across normal/lower by `NUM_FLAT_*` counts

If more entities are requested than catalog entries, extras use `ENTITY_PREFIX` fallback naming.

### Metric Generation (`generate_metric()`)

For each data point at time `dt`:
1. Start with entity baseline ranges (dcount_hosts, events_count)
2. Apply `scale_factor` (per-entity uniqueness)
3. If not flat: apply `DAY_MULTIPLIERS` (Mon=1.0 → Sun=0.6)
4. Apply variation based on `get_effective_behavior()` (±VARIATION_PCT%)
5. If not flat: apply `hourly_multiplier()` (sine or Gaussian curve)
6. Ensure lo ≤ hi and positive values
7. Return random values in adjusted ranges

### Seasonality

**Day-of-week multipliers** (`DAY_MULTIPLIERS`):
```
Monday=1.0, Tuesday=0.92, Wednesday=0.88, Thursday=0.94,
Friday=0.85, Saturday=0.70, Sunday=0.60
```

**Hour-of-day** (`hourly_multiplier()`):
- `curve` mode: `1 + 0.5 * sin((hour - 12) * pi / 12)` — peaks at noon
- `stdev` mode: Gaussian centered at 18h, std_dev=1.5

Both are skipped for flat entities.

### Anomaly Cycling

All outlier entities (regular, short-cycle, flat) cycle between anomaly and normal phases:

**Initialization (in `run_continuous()`):**
All outlier entities start in anomaly immediately after backfill with `is_in_anomaly = True` and a `phase_end_time` set from their duration source.

**Transition logic (in `maybe_transition()`):**
- If `now >= phase_end_time`: switch phase (anomaly→normal or normal→anomaly)
- Duration comes from `_get_anomaly_duration()` / `_get_normal_duration()`
- Short-cycle entities use fixed durations; regular entities pick randomly from global lists

**Duration defaults:**
- Regular anomaly: `[12, 24, 48]` hours (configurable via `ANOMALY_DURATIONS`)
- Regular normal: `[48, 72, 96, 168]` hours (configurable via `NORMAL_DURATIONS`)
- Short-cycle: fixed per-entity (e.g., 2h anomaly / 22h normal)

### Backfill (`run_backfill()`)

- Generates `BACKFILL_DAYS` of data at `BACKFILL_INTERVAL` spacing
- ALL entities use normal behavior (variation=0) to build a clean ML baseline
- Uses deterministic RNG (seed=42) for reproducible historical data
- Progress logged every 10k events

### Continuous Generation (`run_continuous()`)

- Infinite loop at `GENERATION_INTERVAL` spacing
- Checks `maybe_transition()` on every cycle for each outlier entity
- Stats logged every 10 cycles (entity states, sent/failed counts)
- Graceful shutdown via SIGTERM/SIGINT handler (`_running` flag)
- Sleep loop checks `_running` every 1s for responsive shutdown

### DISABLE_ALL_ANOMALIES

When `DISABLE_ALL_ANOMALIES=1`:
- Applied in `main()` after `build_entity_profiles()` but before logging/generation
- Sets `behavior = "normal"` and `short_cycle_hours = None` on ALL entities
- Result: every entity generates clean baseline data, no anomaly cycling occurs
- Useful for generating pure training data without any outliers

### INSTANCE_ID

- If `INSTANCE_ID` env var is set and non-empty: that value is used for all events, and **backfill is skipped** (historical data already exists in Splunk from the previous run)
- If empty (default): `uuid.uuid4()` generates a new UUID per container run, and a full backfill is performed
- The instance_id appears in every event and is used in TrackMe Flex Objects to create unique entities per container run

### Applying `.env` changes

`docker compose restart` reuses the existing container and **does not pick up `.env` changes**. Always use `docker compose up -d` to apply configuration changes (recreates the container). Only rebuild (`docker compose build --no-cache`) when the code itself has changed.

## Event Format

```json
{
    "time": 1697273003,
    "time_human": "Fri Oct 14 10:30:03 2023",
    "dcount_hosts": 742,
    "events_count": 5123456,
    "ref": "security:linux_secure",
    "instance_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

Events are sent to Splunk HEC with:
- `index`: from `SPLUNK_INDEX` (default: `mlgen`)
- `sourcetype`: from `SPLUNK_SOURCETYPE` (default: `_json`)
- `source`: `ml_gen.py` (hardcoded)

## Docker Architecture

- **Base image**: `python:3.13-slim`
- **Non-root user**: `appuser`
- **Unbuffered output**: `PYTHONUNBUFFERED=1` for real-time Docker logs
- **Entrypoint**: `entrypoint.sh` → validates `SPLUNK_HEC_TOKEN`, prints config banner, execs `python3 ml_gen.py`
- **Graceful shutdown**: `stop_grace_period: 30s` in docker-compose.yml
- **Restart policy**: `unless-stopped`
- **Configuration**: entirely via `.env` file (docker-compose `env_file` directive)

## Configuration Reference

All settings via environment variables (`.env` file):

| Variable | Default | Description |
|---|---|---|
| `SPLUNK_HEC_URL` | `https://localhost:8088` | HEC endpoint |
| `SPLUNK_HEC_TOKEN` | *(required)* | HEC auth token |
| `SPLUNK_INDEX` | `mlgen` | Target index |
| `SPLUNK_SOURCETYPE` | `_json` | Event sourcetype |
| `SSL_VERIFY` | `false` | SSL cert verification |
| `BACKFILL_DAYS` | `90` | Historical backfill days |
| `BACKFILL_INTERVAL` | `60` | Seconds between backfill data points |
| `NUM_NORMAL` | `5` | Seasonal normal entities |
| `NUM_LOWER_OUTLIER` | `1` | Seasonal lower outlier entities |
| `NUM_UPPER_OUTLIER` | `1` | Seasonal upper outlier entities |
| `NUM_FLAT_NORMAL` | `1` | Flat normal entities |
| `NUM_FLAT_LOWER_OUTLIER` | `1` | Flat lower outlier entities |
| `VARIATION_PCT` | `75` | Outlier variation (±%) |
| `ANOMALY_DURATIONS` | `12,24,48` | Regular anomaly durations (hours, comma-separated) |
| `NORMAL_DURATIONS` | `48,72,96,168` | Regular normal durations (hours, comma-separated) |
| `ENTITY_PREFIX` | `custom` | Fallback prefix for extra entities |
| `GENERATION_INTERVAL` | `60` | Seconds between continuous data points |
| `HEC_BATCH_SIZE` | `1000` | Events per HEC batch |
| `SEASONALITY_MODE` | `curve` | `curve` (sine/noon) or `stdev` (Gaussian/18h) |
| `INSTANCE_ID` | *(empty)* | Fixed instance ID; if empty, UUID auto-generated |
| `DISABLE_ALL_ANOMALIES` | `0` | `1` = force all entities to normal behavior |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DRY_RUN` | `false` | Generate events without sending to HEC |

## TrackMe Integration

### Flex Objects Tracker Search

The generated data is consumed in TrackMe via a Flex Objects tracker with:
- Root search: `index=mlgen ref=* instance_id=*`
- Object formula: `eval object = "demo" . ":" . instance_id . ":" . ref`
- Alias: `eval alias = ref`
- Stats: `values(instance_id)` as additional KPI

### ML Outlier Detection

- Seasonal entities: use default `time_factor` (day-of-week + hour-of-day)
- Flat entities: set `time_factor=none` in TrackMe since they have no seasonality

### Documentation Reference

TrackMe ML Outlier Detection documentation that references this generator:
`trackme-for-splunk-docs/docs/admin_guide_mloutliers.rst`

## Development Patterns & Pitfalls

### Deterministic RNG
Backfill uses `Random(42)` for reproducibility. Entity profile building also uses `Random(42)`. Continuous generation uses an unseeded `Random()` for natural variation.

### Short-Cycle Entity Isolation
Short-cycle entities are removed from the seasonal catalog pool before random shuffling, preventing them from being double-assigned. If you add a new short-cycle entity, its ref must exist in `ENTITY_CATALOG`.

### HEC Failure Resilience
The generator never crashes on HEC failures. After `MAX_RETRIES` exhausted, events are dropped and the generator continues. This is intentional — the generator should survive network outages and Splunk restarts.

### Anomaly Start Timing
All outlier entities start anomaly immediately after backfill (set directly in `run_continuous()` initialization). The `maybe_transition()` method is NOT used for initial state — previous attempts to use it caused timing bugs where entities started in normal instead of anomaly.

### Flat Entity Behavior
Flat entities skip both `DAY_MULTIPLIERS` and `hourly_multiplier()` in `generate_metric()`. They still support anomaly cycling (variation is applied regardless of flat flag). Their narrow baseline ranges (±5-10%) naturally produce steady volumes.
