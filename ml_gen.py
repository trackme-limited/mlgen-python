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
        # Mode: backfill | normal
        "mode": os.environ.get("MODE", "normal").lower(),
        "backfill_days": int(os.environ.get("BACKFILL_DAYS", "90")),
        # Entity counts by behavior
        "num_normal": int(os.environ.get("NUM_NORMAL", "5")),
        "num_lower_outlier": int(os.environ.get("NUM_LOWER_OUTLIER", "1")),
        "num_upper_outlier": int(os.environ.get("NUM_UPPER_OUTLIER", "1")),
        # Outlier variation percentage (applied as +/- to baseline)
        "variation_pct": int(os.environ.get("VARIATION_PCT", "75")),
        # Generation settings
        "generation_interval": int(os.environ.get("GENERATION_INTERVAL", "60")),
        "backfill_interval": int(os.environ.get("BACKFILL_INTERVAL", "60")),
        "hec_batch_size": int(os.environ.get("HEC_BATCH_SIZE", "1000")),
        # Seasonality mode: curve | stdev
        "seasonality_mode": os.environ.get("SEASONALITY_MODE", "curve").lower(),
        # Entity naming prefix
        "entity_prefix": os.environ.get("ENTITY_PREFIX", "sample"),
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

# Base range templates — each entity will get a random pick scaled from these
BASELINE_TEMPLATES = [
    {"dcount_hosts": (700, 800), "events_count": (5_000_000, 5_500_000)},
    {"dcount_hosts": (600, 700), "events_count": (4_500_000, 5_000_000)},
    {"dcount_hosts": (550, 700), "events_count": (4_000_000, 4_500_000)},
    {"dcount_hosts": (650, 750), "events_count": (4_500_000, 5_000_000)},
    {"dcount_hosts": (500, 600), "events_count": (3_500_000, 4_000_000)},
    {"dcount_hosts": (450, 550), "events_count": (3_000_000, 3_500_000)},
    {"dcount_hosts": (800, 900), "events_count": (6_000_000, 6_500_000)},
    {"dcount_hosts": (350, 450), "events_count": (2_500_000, 3_000_000)},
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


class EntityProfile:
    """Represents a single entity with its own baseline and behavior."""

    def __init__(self, ref: str, behavior: str, baseline: dict, scale_factor: float):
        self.ref = ref
        self.behavior = behavior  # "normal", "lower_outlier", "upper_outlier"
        self.baseline = baseline  # {"dcount_hosts": (lo, hi), "events_count": (lo, hi)}
        self.scale_factor = scale_factor  # Unique per-entity multiplier (0.8 - 1.2)


def build_entity_profiles(config: dict, rng: random.Random) -> list[EntityProfile]:
    """Build entity profiles with assigned behaviors."""
    profiles = []
    entity_num = 1

    for behavior, count in [
        ("normal", config["num_normal"]),
        ("lower_outlier", config["num_lower_outlier"]),
        ("upper_outlier", config["num_upper_outlier"]),
    ]:
        for _ in range(count):
            ref = f"{config['entity_prefix']}_{entity_num:03d}"
            baseline = rng.choice(BASELINE_TEMPLATES)
            scale_factor = rng.uniform(0.8, 1.2)
            profiles.append(EntityProfile(ref, behavior, baseline, scale_factor))
            entity_num += 1

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

    # Apply variation for outlier entities
    effective_variation = 0
    if entity.behavior == "lower_outlier":
        effective_variation = -abs(variation_pct)
    elif entity.behavior == "upper_outlier":
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
    """Generate metrics continuously at the configured interval."""
    global _running

    interval = config["generation_interval"]
    variation_pct = config["variation_pct"]
    seasonality_mode = config["seasonality_mode"]
    rng = random.Random()

    logger.info("Starting continuous generation (interval=%ds)...", interval)
    cycle = 0

    while _running:
        cycle_start = time.time()
        cycle += 1
        now = datetime.now(timezone.utc)

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
            logger.info(
                "Cycle %d [%s]: %d entities | sent: %d | failed: %d",
                cycle, time_tag, len(entities), hec.total_sent, hec.total_failed,
            )

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
    logger.info("  Mode:               %s", config["mode"])
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

    # Run backfill if requested
    if config["mode"] == "backfill":
        run_backfill(config, entities, instance_id, hec, logger)
        logger.info("Backfill complete. Transitioning to continuous generation...")

    # Always run continuous generation after backfill (or directly)
    run_continuous(config, entities, instance_id, hec, logger)

    # Final flush
    hec.flush()
    logger.info(
        "ML Gen stopped. Total sent: %d, Total failed: %d",
        hec.total_sent, hec.total_failed,
    )


if __name__ == "__main__":
    main()
