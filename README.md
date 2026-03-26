# ML Gen — Synthetic Metrics Generator for TrackMe

See: https://docs.trackme-solutions.com/

## Overview

ML Gen generates synthetic time-series data for testing and qualifying TrackMe's Machine Learning Outlier Detection. It produces two key metrics (`dcount_hosts` and `events_count`) with realistic seasonality patterns (day-of-week and hour-of-day variations) and sends them to Splunk via HEC.

### Key Features

- **Multi-entity**: generates data for multiple entities simultaneously, each with unique baselines and scale factors
- **Mixed behaviors**: entities can run in normal mode (clean baseline), lower outlier mode (dip), or upper outlier mode (spike)
- **Anomaly cycling**: outlier entities automatically alternate between anomaly and normal phases, simulating realistic incident lifecycles
- **Short-cycle entities**: three entities with short daily anomaly/recovery cycles for live demos
- **Flat entities**: IT ops / metrics collection entities with no seasonality for `time_factor=none` demos
- **Behavior-change entities**: progressive data onboarding ramp-up followed by a baseline shift — demonstrates ML model retraining / adaptation
- **Instance tracking**: each container run gets a unique `instance_id` (auto-generated or fixed via config) — restart the container to get a fresh instance for TrackMe
- **Backfill support**: populate historical data (default: 90 days) for ML model training, then automatically transitions to continuous generation
- **HEC resilience**: retry with exponential backoff on HEC failures — the generator survives transient outages without crashing
- **Docker-first**: runs as a container via Docker Compose, configured entirely through a `.env` file

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/trackme-limited/mlgen-python.git
cd mlgen-python
cp .env.example .env
# Edit .env with your HEC endpoint, token, and desired settings
```

### 2. Run with Docker Compose

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop
docker compose down
```

### 3. Applying `.env` changes

The `.env` file is read at **container creation** time, not at build time. After editing `.env`:

```bash
# Recreate the container with the updated .env (no rebuild needed)
docker compose up -d
```

> **Important**: `docker compose restart` reuses the existing container and **will not pick up `.env` changes**. Always use `docker compose up -d` to apply configuration changes.

Only rebuild (`docker compose build --no-cache`) when the **code** itself has changed (`ml_gen.py`, `Dockerfile`, `entrypoint.sh`).

### 4. Common workflows

```bash
# Start fresh: new auto-generated instance_id + full backfill
docker compose up -d

# Pin an instance_id after a run (edit .env, set INSTANCE_ID=<value>)
# Then recreate — skips backfill, reuses existing Splunk data
docker compose up -d

# Disable all anomalies mid-run (edit .env, set DISABLE_ALL_ANOMALIES=1)
docker compose up -d

# Re-enable anomalies (edit .env, set DISABLE_ALL_ANOMALIES=0)
docker compose up -d

# Rebuild after code changes (bypass Docker cache)
docker compose build --no-cache && docker compose up -d
```

### 5. What happens on startup

The generator is fully automated — no mode switching needed:

1. **Backfill phase**: generates `BACKFILL_DAYS` (default: 90) of historical data for all entities using **normal behavior** (clean baseline for ML model training). **Skipped** when `INSTANCE_ID` is set (data already exists in Splunk).
2. **Gap fill**: any time gap between the end of backfill and the start of continuous generation is automatically filled so there are no missing data points in Splunk.
3. **Continuous phase**: automatically transitions to real-time generation where each entity follows its assigned behavior — normal entities stay normal, outlier entities immediately start producing anomalies with automatic cycling

To start fresh, remove `INSTANCE_ID` from `.env` and run `docker compose up -d` — you get a new auto-generated `instance_id` and a clean backfill.

## Configuration

All settings are controlled via the `.env` file (copy from `.env.example`):

### Splunk HEC Connection

| Variable | Default | Description |
|---|---|---|
| `SPLUNK_HEC_URL` | `https://localhost:8088` | Splunk HEC endpoint URL |
| `SPLUNK_HEC_TOKEN` | *(required)* | HEC authentication token |
| `SPLUNK_INDEX` | `mlgen` | Target Splunk index |
| `SPLUNK_SOURCETYPE` | `_json` | Event sourcetype |
| `SSL_VERIFY` | `false` | Verify SSL certificates |

### Backfill

| Variable | Default | Description |
|---|---|---|
| `BACKFILL_DAYS` | `90` | Days of historical data to backfill on startup |
| `BACKFILL_INTERVAL` | `60` | Seconds between data points during backfill |

### Entity Counts

| Variable | Default | Description |
|---|---|---|
| `NUM_NORMAL` | `5` | Seasonal entities generating normal (baseline) data |
| `NUM_LOWER_OUTLIER` | `1` | Seasonal entities generating lower-bound outliers |
| `NUM_UPPER_OUTLIER` | `1` | Seasonal entities generating upper-bound outliers |
| `NUM_FLAT_NORMAL` | `1` | Flat entities (no seasonality) generating normal data |
| `NUM_FLAT_LOWER_OUTLIER` | `1` | Flat entities (no seasonality) generating lower-bound outliers |
| `NUM_BEHAVIOR_CHANGE` | `1` | Entities with progressive ramp-up then baseline shift (for ML adaptation demos) |

In addition to the above, 3 short-cycle entities are always included (see [Short-Cycle Entities](#short-cycle-entities-daily-anomalyrecovery) below).

### Anomaly Behavior

| Variable | Default | Description |
|---|---|---|
| `VARIATION_PCT` | `75` | Variation percentage for outlier entities (e.g., 75 = ±75% from baseline) |
| `ANOMALY_DURATIONS` | `12,24,48` | Comma-separated hours — how long each regular anomaly lasts (random pick) |
| `NORMAL_DURATIONS` | `48,72,96,168` | Comma-separated hours — how long normal lasts between regular anomalies (random pick) |

### Generation Settings

| Variable | Default | Description |
|---|---|---|
| `GENERATION_INTERVAL` | `60` | Seconds between data points in continuous mode |
| `HEC_BATCH_SIZE` | `1000` | Events per HEC batch |
| `SEASONALITY_MODE` | `curve` | `curve` (sine wave peaking at noon) or `stdev` (Gaussian peaking at 18h) |
| `ENTITY_PREFIX` | `custom` | Fallback prefix for extra entities beyond the built-in catalog names |

### Instance & Control

| Variable | Default | Description |
|---|---|---|
| `INSTANCE_ID` | *(empty)* | Fixed instance ID for all events. If empty (default), a new UUID is generated each time the container starts. Set this to keep the same instance across restarts. |
| `DISABLE_ALL_ANOMALIES` | `0` | Set to `1` to force ALL entities to normal behavior — no anomaly cycles will run. Useful for generating a clean baseline without any outliers. |

### Logging & Debug

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DRY_RUN` | `false` | Generate events without sending to HEC (for testing) |

## Event Format

Each event sent to Splunk looks like:

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

| Field | Description |
|---|---|
| `time` | Unix timestamp |
| `time_human` | Human-readable timestamp |
| `dcount_hosts` | Simulated distinct host count |
| `events_count` | Simulated event volume |
| `ref` | Entity identifier (e.g., `security:linux_secure`) — use this as the entity key in TrackMe |
| `instance_id` | Container run identifier — auto-generated UUID or fixed value from `INSTANCE_ID` config |

## Entity Types

### Seasonal entities (with day/hour patterns)

Drawn from a built-in catalog of 20 realistic Splunk data source names (e.g., `network:pan:traffic`, `cloud:aws:cloudtrail`, `wineventlog:WinEventLog:System`). Each entity has a unique baseline range and scale factor.

- **Normal** (`NUM_NORMAL`): generates baseline data with realistic day-of-week and hour-of-day seasonality. These entities always produce clean data.
- **Lower outlier** (`NUM_LOWER_OUTLIER`): simulates data drops, resource depletion, or feed failures (metrics reduced by `VARIATION_PCT`%).
- **Upper outlier** (`NUM_UPPER_OUTLIER`): simulates data surges, log storms, or duplicate ingestion (metrics increased by `VARIATION_PCT`%).

If you configure more seasonal entities than the catalog has (17 available after short-cycle entities are reserved), extras use the `ENTITY_PREFIX` fallback naming.

### Flat entities (no seasonality)

Drawn from a catalog of 8 IT ops / metrics collection names (e.g., `metrics:collectd`, `metrics:telegraf`, `itops:snmp:traps`). These produce steady, near-constant volumes regardless of time of day or day of week, with only small natural fluctuations (±5-10%).

- **Normal** (`NUM_FLAT_NORMAL`): steady baseline — like infrastructure monitoring metrics collection.
- **Lower outlier** (`NUM_FLAT_LOWER_OUTLIER`): same flat profile but generates anomalously low values — simulates loss of metrics collectors, network transport failures, or systems going offline.

Flat entities are useful for demonstrating ML outlier detection on KPIs where the TrackMe `time_factor` setting should be set to `none`, since the data doesn't follow time-of-day patterns.

### Behavior-Change Entities (ML Adaptation Demo)

These entities simulate **progressive data onboarding followed by a baseline shift**, a common real-world scenario where volume patterns change over time:

1. **Ramp-up phase** (first ~60 days of backfill): volume grows linearly from 30% to 100% of baseline — simulates a new data source being progressively onboarded
2. **Shift phase** (last ~30 days of backfill + continuous mode): volume jumps to 130% of baseline — simulates the source fully onboarded with slightly higher-than-expected volume

The ML model trains on the full 90-day history (including the ramp), so it sees the recent elevated volume as anomalous. This is the perfect use case for demonstrating **model retraining / training adaptation** in TrackMe — the model needs to be retrained to accept the new normal.

Drawn from the `BEHAVIOR_CHANGE_CATALOG` (default entity: `cloud:gcp:pubsub:audit`). Configure with `NUM_BEHAVIOR_CHANGE`.

### Short-Cycle Entities (Daily Anomaly/Recovery)

Three entities are hardcoded with **short anomaly cycles** that repeat once per day. These are ideal for live demos where you need to see anomaly detection and recovery within hours:

| Entity | Anomaly | Normal | Behavior |
|---|---|---|---|
| `web:access_combined` | 2h | 22h | Lower outlier |
| `cloud:mscs:azure:eventhub` | 1h | 23h | Lower outlier |
| `endpoint:XmlWinEventLog:Microsoft-Windows-Sysmon/Operational` | 4h | 20h | Lower outlier |

These start in anomaly immediately after backfill and cycle daily (anomaly → normal → anomaly...). They are always included regardless of `NUM_*` settings, and are separate from the regular seasonal outlier entities which have longer cycles.

## Anomaly Cycling

Outlier entities don't generate anomalies permanently — they automatically cycle between **anomaly** and **normal** phases to simulate realistic incident lifecycles:

1. After backfill completes, **all outlier entities start in anomaly immediately**
2. The anomaly lasts for a duration from `ANOMALY_DURATIONS` (default: 12, 24, or 48 hours) — or the fixed short-cycle duration
3. The anomaly resolves and they return to **normal** for a duration from `NORMAL_DURATIONS` (default: 48, 72, 96, or 168 hours) — or `24 - anomaly_hours` for short-cycle entities
4. The cycle repeats indefinitely

This reproduces real-world scenarios where issues occur, get addressed, the entity returns to a healthy state, and eventually another incident happens.

During **backfill**, all entities (including outlier ones) generate normal data to ensure a clean ML training baseline.

Set `DISABLE_ALL_ANOMALIES=1` to force all entities to normal behavior — useful for generating pure training data.

## Instance ID

The `instance_id` field identifies a container run:

- **Auto-generated** (default): a new UUID is created each time the container starts. Restarting the container creates fresh entities in TrackMe. A full backfill is performed on startup.
- **Fixed** (`INSTANCE_ID=<value>`): the same value is used across restarts. **Backfill is automatically skipped** since the historical data already exists in Splunk from the previous run. Useful when you want to preserve entity continuity in TrackMe.

A typical workflow is to let the first run auto-generate an `instance_id`, copy it from the logs, then pin it in `.env` for subsequent restarts (so you keep the same entities without re-backfilling).

In TrackMe, `instance_id` is used in the Flex Objects tracker formula to create unique entities per run (e.g., `object = "demo" . ":" . instance_id . ":" . ref`).

## Seasonality Patterns

The generator simulates realistic data patterns for seasonal entities:

- **Day-of-week**: Monday is peak (1.0x), weekdays taper slightly, Saturday (0.7x) and Sunday (0.6x) are lowest
- **Hour-of-day** (two modes):
  - `curve`: sine wave peaking at noon — good for business-hours patterns
  - `stdev`: Gaussian peaking at 18h — good for end-of-day batch patterns

Flat entities skip all seasonality — they produce steady volumes around their baseline.

## HEC Resilience

The generator is designed to survive HEC failures:

- **Retry with exponential backoff**: failed sends are retried up to 5 times with 2s / 4s / 8s / 16s / 32s delays
- **Graceful degradation**: if all retries fail, events are dropped with a warning and the generator continues running
- **No crash on network errors**: connection refused, timeouts, and HTTP errors are all handled
- **Graceful shutdown**: SIGTERM/SIGINT triggers a clean shutdown with queue drain

## Splunk Setup

Ensure Splunk is configured to receive the data:

1. Create the target index (default: `mlgen`)
2. Configure an HEC token with access to the target index
3. Splunk will auto-parse the `_json` sourcetype and extract fields at search time

## License

Copyright 2026, TrackMe Limited, U.K. All rights reserved.
