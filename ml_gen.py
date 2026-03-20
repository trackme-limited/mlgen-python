#!/usr/bin/env python3
"""
ML Gen — Synthetic metrics generator for TrackMe ML Outlier Detection testing.

Generates realistic time-series data (dcount_hosts, events_count) with seasonality
patterns for multiple entities, sending them to Splunk via HEC.

Entities can run in normal mode (baseline), lower_outlier mode (dip), or
upper_outlier mode (spike) to test outlier detection thresholds.

Each container run gets a unique instance_id, so restarting the container
creates a fresh instance that can be tracked independently in TrackMe.
"""

import json
import logging
import math
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__author__ = "TrackMe Limited"
__copyright__ = "Copyright 2026, TrackMe Limited, U.K."
__credits__ = ["Guilhem Marchand"]
__license__ = "TrackMe Limited, all rights reserved"
__version__ = "2.0.0"
__maintainer__ = "TrackMe Limited, U.K."
__email__ = "support@trackme-solutions.com"
__status__ = "PRODUCTION"

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load configuration from environment variables."""
    config = {
        "hec_url": os.environ.get("SPLUNK_HEC_URL", "https://localhost:8088"),
        "hec_token": os.environ.get("SPLUNK_HEC_TOKEN", ""),
        "index": os.environ.get("SPLUNK_INDEX", "mlgen"),
        "sourcetype": os.environ.get("SPLUNK_SOURCETYPE", "_json"),
        # Backfill settings
        "backfill_days": int(os.environ.get("BACKFILL_DAYS", "90")),
        # Entity counts by behavior
        "num_normal": int(os.environ.get("NUM_NORMAL", "5")),
        "num_lower_outlier": int(os.environ.get("NUM_LOWER_OUTLIER", "1")),
        "num_upper_outlier": int(os.environ.get("NUM_UPPER_OUTLIER", "1")),
        # Outlier variation percentage (applied as +/- to baseline)
        "variation_pct": int(os.environ.get("VARIATION_PCT", "75")),
        # Anomaly cycling: comma-separated hours for anomaly and normal durations
        "anomaly_durations": [
            int(h) for h in os.environ.get("ANOMALY_DURATIONS", "12,24,48").split(",")
        ],
        "normal_durations": [
            int(h) for h in os.environ.get("NORMAL_DURATIONS", "48,72,96,168").split(",")
        ],
        # Generation settings
        "generation_interval": int(os.environ.get("GENERATION_INTERVAL", "60")),
        "backfill_interval": int(os.environ.get("BACKFILL_INTERVAL", "60")),
        "hec_batch_size": int(os.environ.get("HEC_BATCH_SIZE", "1000")),
        # Seasonality mode: curve | stdev
        "seasonality_mode": os.environ.get("SEASONALITY_MODE", "curve").lower(),
        # Entity naming fallback prefix (used when more entities than catalog entries)
        "entity_prefix": os.environ.get("ENTITY_PREFIX", "custom"),
        # Logging
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),
        "ssl_verify": os.environ.get("SSL_VERIFY", "false").lower() in ("true", "1", "yes"),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
    }

    if not config["hec_token"] and not config["dry_run"]:
        print(
            "ERROR: SPLUNK_HEC_TOKEN is required (or set DRY_RUN=true).",
            file=sys.stderr,
        )
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(level: str):
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# Entity profiles
# ---------------------------------------------------------------------------

# Each entity gets its own baseline ranges (randomized at startup) so that
# entities look distinct from each other in Splunk.

# Realistic entity names that look like real Splunk data sources (index:sourcetype)
# Each entity is paired with a baseline template for unique data profiles.
ENTITY_CATALOG = [
    # High-volume security feeds
    {"ref": "security:linux_secure", "dcount_hosts": (700, 800), "events_count": (5_000_000, 5_500_000)},
    {"ref": "security:WinEventLog:Security", "dcount_hosts": (800, 900), "events_count": (6_000_000, 6_500_000)},
    {"ref": "network:cisco:asa", "dcount_hosts": (350, 450), "events_count": (4_000_000, 4_500_000)},
    {"ref": "network:pan:traffic", "dcount_hosts": (250, 350), "events_count": (3_500_000, 4_000_000)},
    # Mid-volume infra feeds
    {"ref": "osnix:linux:audit", "dcount_hosts": (600, 700), "events_count": (4_500_000, 5_000_000)},
    {"ref": "web:access_combined", "dcount_hosts": (450, 550), "events_count": (3_000_000, 3_500_000)},
    {"ref": "infra:vmware:esxlog:hostd", "dcount_hosts": (150, 250), "events_count": (2_000_000, 2_500_000)},
    {"ref": "infra:syslog", "dcount_hosts": (550, 700), "events_count": (4_000_000, 4_500_000)},
    # Cloud & identity
    {"ref": "cloud:aws:cloudtrail", "dcount_hosts": (100, 200), "events_count": (2_500_000, 3_000_000)},
    {"ref": "cloud:mscs:azure:eventhub", "dcount_hosts": (200, 300), "events_count": (3_000_000, 3_500_000)},
    {"ref": "cloud:o365:management:activity", "dcount_hosts": (50, 120), "events_count": (1_500_000, 2_000_000)},
    {"ref": "identity:OktaIM2:log", "dcount_hosts": (30, 80), "events_count": (800_000, 1_200_000)},
    # Windows & endpoints
    {"ref": "wineventlog:WinEventLog:System", "dcount_hosts": (650, 750), "events_count": (4_500_000, 5_000_000)},
    {"ref": "wineventlog:WinEventLog:Application", "dcount_hosts": (500, 600), "events_count": (3_500_000, 4_000_000)},
    {"ref": "endpoint:XmlWinEventLog:Microsoft-Windows-Sysmon/Operational", "dcount_hosts": (400, 500), "events_count": (5_500_000, 6_000_000)},
    # Application & business
    {"ref": "applog:kafka:broker", "dcount_hosts": (20, 50), "events_count": (2_000_000, 2_500_000)},
    {"ref": "applog:docker:container:json", "dcount_hosts": (300, 400), "events_count": (3_000_000, 3_500_000)},
    {"ref": "database:oracle:audit", "dcount_hosts": (10, 30), "events_count": (500_000, 800_000)},
    {"ref": "proxy:zscalernss-web", "dcount_hosts": (200, 350), "events_count": (4_000_000, 4_500_000)},
    {"ref": "email:exchange:messagetracking", "dcount_hosts": (40, 80), "events_count": (1_000_000, 1_500_000)},
]

# Day-of-week multipliers (relative to Monday baseline)
DAY_MULTIPLIERS = {
    "Monday": 1.0,
    "Tuesday": 0.92,
    "Wednesday": 0.88,
    "Thursday": 0.94,
    "Friday": 0.85,
    "Saturday": 0.70,
    "Sunday": 0.60,
}

# Anomaly cycling durations (in hours)
# Outlier entities pick random durations from these lists to simulate
# realistic incident lifecycles: anomaly happens, gets fixed, normal for
# a while, then another anomaly occurs.
DEFAULT_ANOMALY_DURATIONS = [12, 24, 48]          # How long an anomaly lasts
DEFAULT_NORMAL_DURATIONS = [48, 72, 96, 168]     # How long normal lasts between anomalies


class EntityProfile:
    """Represents a single entity with its own baseline and behavior."""

    def __init__(self, ref: str, behavior: str, baseline: dict, scale_factor: float):
        self.ref = ref
        self.behavior = behavior  # "normal", "lower_outlier", "upper_outlier"
        self.baseline = baseline  # {"dcount_hosts": (lo, hi), "events_count": (lo, hi)}
        self.scale_factor = scale_factor  # Unique per-entity multiplier (0.8 - 1.2)

        # Anomaly cycling state (only used for outlier entities in continuous mode)
        self.is_in_anomaly = False
        self.phase_end_time: datetime | None = None  # When the current phase ends

    def is_outlier(self) -> bool:
        """Whether this entity is configured as an outlier (lower or upper)."""
        return self.behavior in ("lower_outlier", "upper_outlier")

    def get_effective_behavior(self) -> str:
        """Return the current effective behavior, accounting for anomaly cycling.

        Outlier entities alternate between anomaly and normal phases.
        Normal entities always return 'normal'.
        """
        if not self.is_outlier():
            return "normal"
        return self.behavior if self.is_in_anomaly else "normal"

    def maybe_transition(self, now: datetime, rng: random.Random, logger: logging.Logger,
                         anomaly_durations: list[int], normal_durations: list[int],
                         start_in_anomaly: bool = False):
        """Check if it's time to transition between anomaly and normal phases."""
        if not self.is_outlier():
            return

        # First call — initialize the phase
        if self.phase_end_time is None:
            if start_in_anomaly:
                # Start directly in anomaly phase (no waiting)
                duration_hours = rng.choice(anomaly_durations)
                self.is_in_anomaly = True
                self.phase_end_time = now + timedelta(hours=duration_hours)
                logger.info(
                    "  Entity %s: starting immediately in %s anomaly for %dh",
                    self.ref, self.behavior, duration_hours,
                )
            else:
                # Start in normal phase, anomaly comes later
                initial_hours = rng.choice(normal_durations)
                self.phase_end_time = now + timedelta(hours=initial_hours)
                self.is_in_anomaly = False
                logger.info(
                    "  Entity %s: starting in normal phase, first anomaly in %dh",
                    self.ref, initial_hours,
                )
            return

        if now < self.phase_end_time:
            return  # Still in current phase

        # Time to transition
        if self.is_in_anomaly:
            # Anomaly → Normal (issue was "fixed")
            duration_hours = rng.choice(normal_durations)
            self.is_in_anomaly = False
            self.phase_end_time = now + timedelta(hours=duration_hours)
            logger.info(
                "  Entity %s: anomaly resolved — back to normal for %dh",
                self.ref, duration_hours,
            )
        else:
            # Normal → Anomaly (new incident)
            duration_hours = rng.choice(anomaly_durations)
            self.is_in_anomaly = True
            self.phase_end_time = now + timedelta(hours=duration_hours)
            logger.info(
                "  Entity %s: new %s anomaly started — will last %dh",
                self.ref, self.behavior, duration_hours,
            )


def build_entity_profiles(config: dict, rng: random.Random) -> list[EntityProfile]:
    """Build entity profiles with assigned behaviors.

    Entities are picked from ENTITY_CATALOG to get realistic names that look
    like real Splunk index:sourcetype combinations. If more entities are needed
    than available in the catalog, extras are generated with a prefix.
    """
    total_needed = config["num_normal"] + config["num_lower_outlier"] + config["num_upper_outlier"]

    # Shuffle the catalog and pick entities
    catalog = list(ENTITY_CATALOG)
    rng.shuffle(catalog)

    profiles = []
    entity_idx = 0

    for behavior, count in [
        ("normal", config["num_normal"]),
        ("lower_outlier", config["num_lower_outlier"]),
        ("upper_outlier", config["num_upper_outlier"]),
    ]:
        for _ in range(count):
            if entity_idx < len(catalog):
                entry = catalog[entity_idx]
                ref = entry["ref"]
                baseline = {
                    "dcount_hosts": entry["dcount_hosts"],
                    "events_count": entry["events_count"],
                }
            else:
                # Fallback for when we need more entities than the catalog has
                ref = f"{config['entity_prefix']}_{entity_idx + 1:03d}"
                baseline = rng.choice([
                    {"dcount_hosts": e["dcount_hosts"], "events_count": e["events_count"]}
                    for e in ENTITY_CATALOG
                ])

            scale_factor = rng.uniform(0.8, 1.2)
            profiles.append(EntityProfile(ref, behavior, baseline, scale_factor))
            entity_idx += 1

    return profiles


# ---------------------------------------------------------------------------
# Metric generation
# ---------------------------------------------------------------------------


def hourly_multiplier(hour: int, mode: str = "curve") -> float:
    """
    Returns a time-of-day multiplier.
    - curve: sine wave peaking at noon
    - stdev: Gaussian peaking at 18h
    """
    if mode == "curve":
        multiplier = 1 + 0.5 * math.sin((hour - 12) * math.pi / 12)
    else:  # stdev
        mean = 18
        std_dev = 1.5
        multiplier = math.exp(-((hour - mean) ** 2) / (2 * std_dev ** 2))
        # Normalize so peak = 1
        peak = 1.0
        multiplier = multiplier / peak

    return max(multiplier, 0.5)


def generate_metric(
    dt: datetime,
    entity: EntityProfile,
    variation_pct: int,
    instance_id: str,
    seasonality_mode: str,
    rng: random.Random,
) -> dict:
    """Generate a single metric data point for an entity at a given time."""
    weekday = dt.strftime("%A")
    hour = dt.hour

    # Base ranges from entity profile
    dcount_lo, dcount_hi = entity.baseline["dcount_hosts"]
    events_lo, events_hi = entity.baseline["events_count"]

    # Apply entity-unique scale factor
    dcount_lo = int(dcount_lo * entity.scale_factor)
    dcount_hi = int(dcount_hi * entity.scale_factor)
    events_lo = int(events_lo * entity.scale_factor)
    events_hi = int(events_hi * entity.scale_factor)

    # Apply day-of-week seasonality
    day_mult = DAY_MULTIPLIERS.get(weekday, 1.0)
    dcount_lo = int(dcount_lo * day_mult)
    dcount_hi = int(dcount_hi * day_mult)
    events_lo = int(events_lo * day_mult)
    events_hi = int(events_hi * day_mult)

    # Apply variation based on current effective behavior
    # (outlier entities cycle between anomaly and normal phases)
    effective_behavior = entity.get_effective_behavior()
    effective_variation = 0
    if effective_behavior == "lower_outlier":
        effective_variation = -abs(variation_pct)
    elif effective_behavior == "upper_outlier":
        effective_variation = abs(variation_pct)

    if effective_variation != 0:
        factor = 1 + effective_variation / 100.0
        dcount_lo = int(dcount_lo * factor)
        dcount_hi = int(dcount_hi * factor)
        events_lo = int(events_lo * factor)
        events_hi = int(events_hi * factor)

    # Apply hour-of-day multiplier
    h_mult = hourly_multiplier(hour, mode=seasonality_mode)
    dcount_lo = int(dcount_lo * h_mult)
    dcount_hi = int(dcount_hi * h_mult)
    events_lo = int(events_lo * h_mult)
    events_hi = int(events_hi * h_mult)

    # Ensure lo <= hi
    if dcount_lo > dcount_hi:
        dcount_lo, dcount_hi = dcount_hi, dcount_lo
    if events_lo > events_hi:
        events_lo, events_hi = events_hi, events_lo

    # Ensure positive
    dcount_lo = max(1, dcount_lo)
    dcount_hi = max(dcount_lo, dcount_hi)
    events_lo = max(1, events_lo)
    events_hi = max(events_lo, events_hi)

    dcount = rng.randint(dcount_lo, dcount_hi)
    events = rng.randint(events_lo, events_hi)

    return {
        "time": int(dt.timestamp()),
        "time_human": dt.strftime("%c"),
        "dcount_hosts": dcount,
        "events_count": events,
        "ref": entity.ref,
        "instance_id": instance_id,
    }


# ---------------------------------------------------------------------------
# HEC sender
# ---------------------------------------------------------------------------


class HECSender:
    """Batched HEC event sender with retry and exponential backoff.

    Designed to survive transient HEC failures (network issues, Splunk restarts,
    etc.) without crashing. Failed batches are retried with exponential backoff.
    If all retries are exhausted, events are dropped with a warning and the
    generator continues running.
    """

    MAX_RETRIES = 5
    INITIAL_BACKOFF = 2.0  # seconds
    BACKOFF_MULTIPLIER = 2.0
    MAX_BACKOFF = 60.0  # seconds

    def __init__(self, url: str, token: str, index: str, sourcetype: str,
                 batch_size: int, ssl_verify: bool, dry_run: bool):
        self.url = f"{url.rstrip('/')}/services/collector"
        self.token = token
        self.index = index
        self.sourcetype = sourcetype
        self.batch_size = batch_size
        self.ssl_verify = ssl_verify
        self.dry_run = dry_run
        self.session = requests.Session()
        self.queue: list[dict] = []
        self.total_sent = 0
        self.total_failed = 0
        self.total_retries = 0
        self.logger = logging.getLogger("hec")

    def enqueue(self, metric: dict):
        """Add a metric to the queue, flushing when batch size is reached."""
        self.queue.append(metric)
        if len(self.queue) >= self.batch_size:
            self.flush()

    def _send_with_retry(self, payload: str, event_count: int):
        """Send payload to HEC with exponential backoff retry."""
        headers = {"Authorization": f"Splunk {self.token}"}
        backoff = self.INITIAL_BACKOFF

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.session.post(
                    self.url, headers=headers, data=payload,
                    verify=self.ssl_verify, timeout=30,
                )
                if resp.status_code == 200:
                    self.total_sent += event_count
                    if attempt > 1:
                        self.logger.info(
                            "HEC send succeeded on attempt %d (%d events)", attempt, event_count,
                        )
                    return
                else:
                    self.logger.warning(
                        "HEC returned %d on attempt %d/%d: %s",
                        resp.status_code, attempt, self.MAX_RETRIES, resp.text,
                    )
            except requests.exceptions.RequestException as exc:
                self.logger.warning(
                    "HEC request failed on attempt %d/%d: %s",
                    attempt, self.MAX_RETRIES, exc,
                )

            # Don't sleep after the last attempt
            if attempt < self.MAX_RETRIES:
                self.total_retries += 1
                sleep_time = min(backoff, self.MAX_BACKOFF)
                self.logger.info("Retrying in %.1fs...", sleep_time)
                time.sleep(sleep_time)
                backoff *= self.BACKOFF_MULTIPLIER

        # All retries exhausted — drop events but keep running
        self.total_failed += event_count
        self.logger.error(
            "HEC send failed after %d attempts — dropping %d events. "
            "Generator will continue running.",
            self.MAX_RETRIES, event_count,
        )

    def flush(self):
        """Send all queued events to HEC."""
        if not self.queue:
            return

        event_count = len(self.queue)

        if self.dry_run:
            self.logger.debug("DRY RUN: would send %d events", event_count)
            self.total_sent += event_count
            self.queue = []
            return

        payload = "".join(
            json.dumps({
                "event": event,
                "index": self.index,
                "sourcetype": self.sourcetype,
                "source": "ml_gen.py",
                "time": event.get("time"),
            })
            for event in self.queue
        )

        self.queue = []
        self._send_with_retry(payload, event_count)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def run_backfill(
    config: dict,
    entities: list[EntityProfile],
    instance_id: str,
    hec: HECSender,
    logger: logging.Logger,
):
    """Generate historical data for all entities."""
    days = config["backfill_days"]
    interval = config["backfill_interval"]
    variation_pct = config["variation_pct"]
    seasonality_mode = config["seasonality_mode"]

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    total_points = int((days * 86400) / interval)
    logger.info(
        "Starting backfill: %d days, %ds interval, ~%d points per entity, %d entities = ~%d total events",
        days, interval, total_points, len(entities), total_points * len(entities),
    )

    rng = random.Random(42)  # Deterministic for reproducible backfill
    current_time = start_time

    event_count = 0
    while current_time <= end_time:
        for entity in entities:
            # During backfill, all entities use normal behavior (variation=0)
            # to build a clean baseline for ML training
            metric = generate_metric(
                current_time, entity, 0, instance_id, seasonality_mode, rng,
            )
            hec.enqueue(metric)
            event_count += 1

        current_time += timedelta(seconds=interval)

        # Progress logging every 10k events
        if event_count % 10000 < len(entities):
            pct = ((current_time - start_time) / (end_time - start_time)) * 100
            logger.info(
                "Backfill progress: %.1f%% — %d events sent, %d failed",
                pct, hec.total_sent, hec.total_failed,
            )

    hec.flush()
    logger.info(
        "Backfill complete: %d events sent, %d failed",
        hec.total_sent, hec.total_failed,
    )


# ---------------------------------------------------------------------------
# Continuous generation
# ---------------------------------------------------------------------------

_running = True


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _running
    sig_name = signal.Signals(signum).name
    logging.getLogger("ml_gen").info("Received %s — shutting down gracefully...", sig_name)
    _running = False


def run_continuous(
    config: dict,
    entities: list[EntityProfile],
    instance_id: str,
    hec: HECSender,
    logger: logging.Logger,
):
    """Generate metrics continuously at the configured interval.

    Outlier entities automatically cycle between anomaly and normal phases:
    - Anomaly phase: entity generates outlier data (lower or upper bound)
    - Normal phase: entity generates clean baseline data (issue "fixed")
    Durations are randomized from ANOMALY_DURATIONS / NORMAL_DURATIONS.
    """
    global _running

    interval = config["generation_interval"]
    variation_pct = config["variation_pct"]
    seasonality_mode = config["seasonality_mode"]
    anomaly_durations = config["anomaly_durations"]
    normal_durations = config["normal_durations"]
    rng = random.Random()

    # Initialize anomaly cycling for outlier entities.
    # ALL outlier entities start immediately in anomaly right after backfill
    # so the effect is visible within minutes. After their first anomaly
    # window expires, each entity cycles independently (anomaly → normal → anomaly...).
    now = datetime.now(timezone.utc)
    logger.info("Initializing anomaly cycling for outlier entities...")
    for entity in entities:
        if entity.is_outlier():
            duration_hours = rng.choice(anomaly_durations)
            entity.is_in_anomaly = True
            entity.phase_end_time = now + timedelta(hours=duration_hours)
            logger.info(
                "  Entity %s: starting %s anomaly NOW — will last %dh",
                entity.ref, entity.behavior, duration_hours,
            )

    logger.info("Starting continuous generation (interval=%ds)...", interval)
    cycle = 0

    while _running:
        cycle_start = time.time()
        cycle += 1
        now = datetime.now(timezone.utc)

        # Check for phase transitions on outlier entities
        for entity in entities:
            entity.maybe_transition(now, rng, logger, anomaly_durations, normal_durations)

        for entity in entities:
            metric = generate_metric(
                now, entity, variation_pct, instance_id, seasonality_mode, rng,
            )
            hec.enqueue(metric)

        hec.flush()

        # Log stats periodically
        if cycle % 10 == 0 or cycle == 1:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            time_tag = f"{day_names[now.weekday()]} {now.hour:02d}:{now.minute:02d} UTC"

            # Show current state of outlier entities
            anomaly_details = []
            for e in entities:
                if e.is_outlier():
                    state = "ANOMALY" if e.is_in_anomaly else "normal"
                    remaining = ""
                    if e.phase_end_time:
                        delta = e.phase_end_time - now
                        remaining = f" ({delta.total_seconds()/3600:.1f}h left)"
                    anomaly_details.append(f"{e.ref}={state}{remaining}")

            logger.info(
                "Cycle %d [%s]: %d entities | sent: %d | failed: %d",
                cycle, time_tag, len(entities), hec.total_sent, hec.total_failed,
            )
            for detail in anomaly_details:
                logger.info("  Outlier: %s", detail)

        # Sleep remainder of interval
        elapsed = time.time() - cycle_start
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0 and _running:
            sleep_end = time.time() + sleep_time
            while time.time() < sleep_end and _running:
                time.sleep(min(1.0, sleep_end - time.time()))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    config = load_config()
    setup_logging(config["log_level"])
    logger = logging.getLogger("ml_gen")

    # Generate a unique instance_id for this container run
    instance_id = str(uuid.uuid4())

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Build entity profiles
    rng = random.Random(42)
    entities = build_entity_profiles(config, rng)

    total_entities = len(entities)
    normal_count = sum(1 for e in entities if e.behavior == "normal")
    lower_count = sum(1 for e in entities if e.behavior == "lower_outlier")
    upper_count = sum(1 for e in entities if e.behavior == "upper_outlier")

    logger.info("=" * 70)
    logger.info("ML Gen — Synthetic Metrics Generator for TrackMe")
    logger.info("=" * 70)
    logger.info("  Instance ID:        %s", instance_id)
    logger.info("  HEC URL:            %s", config["hec_url"])
    logger.info("  Index:              %s", config["index"])
    logger.info("  Sourcetype:         %s", config["sourcetype"])
    logger.info("  Backfill days:      %d", config["backfill_days"])
    logger.info("  Seasonality:        %s", config["seasonality_mode"])
    logger.info("  Entities:           %d total", total_entities)
    logger.info("    Normal:           %d", normal_count)
    logger.info("    Lower outlier:    %d (variation: -%d%%)", lower_count, config["variation_pct"])
    logger.info("    Upper outlier:    %d (variation: +%d%%)", upper_count, config["variation_pct"])
    logger.info("  Generation interval: %ds", config["generation_interval"])
    logger.info("  HEC batch size:     %d", config["hec_batch_size"])
    logger.info("  Dry run:            %s", config["dry_run"])
    logger.info("  SSL verify:         %s", config["ssl_verify"])
    logger.info("=" * 70)

    # Log entity details
    for entity in entities:
        logger.info(
            "  Entity: ref=%s  behavior=%-14s  scale=%.2f  baseline_hosts=%s  baseline_events=%s",
            entity.ref, entity.behavior, entity.scale_factor,
            entity.baseline["dcount_hosts"], entity.baseline["events_count"],
        )

    # Initialize HEC sender
    hec = HECSender(
        url=config["hec_url"],
        token=config["hec_token"],
        index=config["index"],
        sourcetype=config["sourcetype"],
        batch_size=config["hec_batch_size"],
        ssl_verify=config["ssl_verify"],
        dry_run=config["dry_run"],
    )

    # Phase 1: Backfill historical data (all entities use normal behavior)
    run_backfill(config, entities, instance_id, hec, logger)
    logger.info("Backfill complete. Transitioning to continuous generation...")

    # Phase 2: Continuous generation (entities follow their assigned behaviors)
    run_continuous(config, entities, instance_id, hec, logger)

    # Final flush
    hec.flush()
    logger.info(
        "ML Gen stopped. Total sent: %d, Total failed: %d",
        hec.total_sent, hec.total_failed,
    )


if __name__ == "__main__":
    main()
