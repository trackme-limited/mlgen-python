# ML Gen — Synthetic Metrics Generator for TrackMe

See: https://docs.trackme-solutions.com/

## Overview

ML Gen generates synthetic time-series data for testing and qualifying TrackMe's Machine Learning Outlier Detection. It produces two key metrics (`dcount_hosts` and `events_count`) with realistic seasonality patterns (day-of-week and hour-of-day variations) and sends them to Splunk via HEC.

### Key Features

- **Multi-entity**: generates data for multiple entities simultaneously, each with unique baselines
- **Mixed behaviors**: entities can run in normal mode (clean baseline), lower outlier mode (dip), or upper outlier mode (spike)
- **Instance tracking**: each container run gets a unique `instance_id` — restart the container to get a fresh instance for TrackMe
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

# Restart (generates a new instance_id for fresh tracking)
docker compose restart

# Stop
docker compose down
```

### 3. What happens on startup

The generator is fully automated — no mode switching needed:

1. **Backfill phase**: generates `BACKFILL_DAYS` (default: 90) of historical data for all entities using **normal behavior** (clean baseline for ML model training)
2. **Continuous phase**: automatically transitions to real-time generation where each entity follows its assigned behavior — normal entities stay normal, outlier entities start producing anomalies

To start fresh, just restart the container — you get a new `instance_id` and a clean backfill.

## Configuration

All settings are controlled via the `.env` file (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `SPLUNK_HEC_URL` | `https://localhost:8088` | Splunk HEC endpoint URL |
| `SPLUNK_HEC_TOKEN` | *(required)* | HEC authentication token |
| `SPLUNK_INDEX` | `mlgen` | Target Splunk index |
| `SPLUNK_SOURCETYPE` | `_json` | Event sourcetype |
| `SSL_VERIFY` | `false` | Verify SSL certificates |
| `BACKFILL_DAYS` | `90` | Days of historical data to backfill on startup |
| `BACKFILL_INTERVAL` | `60` | Seconds between data points during backfill |
| `NUM_NORMAL` | `5` | Number of entities generating normal (baseline) data |
| `NUM_LOWER_OUTLIER` | `1` | Number of entities generating lower-bound outliers |
| `NUM_UPPER_OUTLIER` | `1` | Number of entities generating upper-bound outliers |
| `VARIATION_PCT` | `75` | Variation percentage for outlier entities (e.g. 75 = +/-75%) |
| `ANOMALY_DURATIONS` | `12,24,48` | Comma-separated hours — how long each anomaly lasts (random pick) |
| `NORMAL_DURATIONS` | `48,72,96,168` | Comma-separated hours — how long normal lasts between anomalies (random pick) |
| `ENTITY_PREFIX` | `custom` | Fallback prefix for extra entities beyond the 20 built-in catalog names |
| `GENERATION_INTERVAL` | `60` | Seconds between data points in continuous mode |
| `HEC_BATCH_SIZE` | `1000` | Events per HEC batch |
| `SEASONALITY_MODE` | `curve` | `curve` (sine wave peaking at noon) or `stdev` (Gaussian peaking at 18h) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DRY_RUN` | `false` | Generate events without sending to HEC |

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
| `ref` | Entity identifier (e.g. `security:linux_secure`) — use this as the entity key in TrackMe |
| `instance_id` | UUID generated at container start — changes on every restart |

Entity names are drawn from a built-in catalog of 20 realistic Splunk data sources (e.g. `network:pan:traffic`, `cloud:aws:cloudtrail`, `wineventlog:WinEventLog:System`). If you configure more entities than the catalog has, extras use the `ENTITY_PREFIX` fallback.

## Entity Behaviors

- **Normal** (`NUM_NORMAL`): generates baseline data with realistic day-of-week and hour-of-day seasonality. These entities always produce clean data.
- **Lower outlier** (`NUM_LOWER_OUTLIER`): simulates data drops, resource depletion, or feed failures (metrics reduced by `VARIATION_PCT`%).
- **Upper outlier** (`NUM_UPPER_OUTLIER`): simulates data surges, log storms, or duplicate ingestion (metrics increased by `VARIATION_PCT`%).

Each entity gets a unique baseline range and scale factor, so they look distinct in Splunk dashboards.

### Anomaly Cycling

Outlier entities don't generate anomalies permanently — they automatically cycle between **anomaly** and **normal** phases to simulate realistic incident lifecycles:

1. After backfill completes, outlier entities start in a short **normal phase** (2-8h random)
2. Then they enter an **anomaly phase** for a random duration picked from `ANOMALY_DURATIONS` (default: 12, 24, or 48 hours)
3. The anomaly resolves and they return to **normal** for a duration from `NORMAL_DURATIONS` (default: 48, 72, 96, or 168 hours)
4. The cycle repeats indefinitely

This reproduces real-world scenarios where issues occur, get addressed, the entity comes back to a healthy state, and eventually another incident happens. The periodic logging shows which entities currently have active anomalies.

During **backfill**, all entities (including outlier ones) generate normal data to ensure a clean ML training baseline.

## Instance ID

The `instance_id` field is a UUID generated once when the container starts. This means:

- **Same container run** = same `instance_id` across all entities and all events
- **Restart the container** = new `instance_id`
- Use the `instance_id` in TrackMe to distinguish between different test runs
- Combined with `ref` (entity name), you can filter to a specific entity within a specific run

## Seasonality Patterns

The generator simulates realistic data patterns:

- **Day-of-week**: Monday is peak (1.0x), weekdays taper slightly, Saturday (0.7x) and Sunday (0.6x) are lowest
- **Hour-of-day** (two modes):
  - `curve`: sine wave peaking at noon — good for business-hours patterns
  - `stdev`: Gaussian peaking at 18h — good for end-of-day batch patterns

## HEC Resilience

The generator is designed to survive HEC failures:

- **Retry with exponential backoff**: failed sends are retried up to 5 times with 2s / 4s / 8s / 16s / 32s delays
- **Graceful degradation**: if all retries fail, events are dropped with a warning and the generator continues
- **No crash on network errors**: connection refused, timeouts, and HTTP errors are all handled
- **Graceful shutdown**: SIGTERM/SIGINT triggers a clean shutdown with queue drain

## Splunk Setup

Ensure Splunk is configured to receive the data:

1. Create the target index (default: `mlgen`)
2. Configure an HEC token with access to the target index
3. Splunk will auto-parse the `_json` sourcetype and extract fields at search time
