import time
import random
import json
import argparse
from datetime import datetime, timedelta
import requests
import math

# Disable warnings for insecure requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_BATCH_SIZE = 1000  # Maximum events in a single batch
event_queue = []  # A queue to hold the events
LAST_BATCH_SENT = datetime.now()


def hourly_multiplier(hour, weekday, mode="curve"):
    """
    Given an hour (0 to 23) and a mode, this function returns a multiplier.
    The curve mode uses a sine wave.
    The stdev mode uses a Gaussian-like curve centered at 18h for weekdays.
    """
    if mode == "curve":
        # Use a sine wave centered at midday for a simple increase and decrease
        multiplier = 1 + 0.5 * math.sin((hour - 12) * math.pi / 12)

    else:  # mode is "stdev"
        # Use the Gaussian curve with a mean at 18h
        mean = 18
        std_dev = 1.5
        multiplier = (1 / (std_dev * (2 * 3.141592653589793) ** 0.5)) * (
            2.718281828459045 ** -((hour - mean) ** 2 / (2 * std_dev**2))
        )

        # Normalize the multiplier to ensure the peak is 1
        peak_value = 1 / (std_dev * (2 * 3.141592653589793) ** 0.5)
        multiplier /= peak_value

    # Ensure the multiplier is not too low
    multiplier = max(multiplier, 0.5)

    return multiplier


def adjust_range(value_range, variation_pct):
    """Adjusts a range (tuple of two values) by a given percentage."""
    lower_bound = value_range[0] * (1 - variation_pct / 100.0)
    upper_bound = value_range[1] * (1 + variation_pct / 100.0)

    # Ensure lower bound doesn't exceed upper bound
    if lower_bound > upper_bound:
        lower_bound, upper_bound = upper_bound, lower_bound

    return (int(lower_bound), int(upper_bound))


def get_day_ranges(day, variation_pct=0):
    original_ranges = {
        "Monday": {"dcount_hosts": (700, 800), "events_count": (5000000, 5500000)},
        "Tuesday": {"dcount_hosts": (600, 700), "events_count": (4500000, 5000000)},
        "Wednesday": {"dcount_hosts": (550, 700), "events_count": (4000000, 4500000)},
        "Thursday": {"dcount_hosts": (650, 750), "events_count": (4500000, 5000000)},
        "Friday": {"dcount_hosts": (550, 650), "events_count": (4000000, 4500000)},
        "Saturday": {"dcount_hosts": (500, 600), "events_count": (3500000, 4000000)},
        "Sunday": {"dcount_hosts": (400, 450), "events_count": (3000000, 3500000)},
    }

    adjusted_ranges = {
        key: {
            inner_key: adjust_range(value, variation_pct)
            for inner_key, value in inner_dict.items()
        }
        for key, inner_dict in original_ranges.items()
    }
    return adjusted_ranges[day]


def generate_metric_for_time(dt, variation_pct, ref_sample, mode):
    weekday = dt.strftime("%A")
    hour = dt.hour

    # Get the original ranges without variation
    original_ranges = get_day_ranges(weekday)

    # Adjust for variation percentage
    adjusted_dcount_lower = int(
        original_ranges["dcount_hosts"][0] * (1 + variation_pct / 100.0)
    )
    adjusted_dcount_upper = int(
        original_ranges["dcount_hosts"][1] * (1 + variation_pct / 100.0)
    )

    adjusted_events_lower = int(
        original_ranges["events_count"][0] * (1 + variation_pct / 100.0)
    )
    adjusted_events_upper = int(
        original_ranges["events_count"][1] * (1 + variation_pct / 100.0)
    )

    # Now adjust these for the hour of the day
    multiplier = hourly_multiplier(hour, weekday, mode=mode)  # <-- Passing mode here

    adjusted_dcount_lower = int(adjusted_dcount_lower * multiplier)
    adjusted_dcount_upper = int(adjusted_dcount_upper * multiplier)

    adjusted_events_lower = int(adjusted_events_lower * multiplier)
    adjusted_events_upper = int(adjusted_events_upper * multiplier)

    dcount = random.randint(adjusted_dcount_lower, adjusted_dcount_upper)
    events = random.randint(adjusted_events_lower, adjusted_events_upper)

    return {
        "time": int(dt.timestamp()),
        "dcount_hosts": dcount,
        "events_count": events,
        "ref_sample": ref_sample,
    }


def add_event_to_queue(metric, target, token, index, sourcetype):
    global event_queue
    global LAST_BATCH_SENT
    event_queue.append(metric)
    if (
        len(event_queue) >= MAX_BATCH_SIZE
        or (datetime.now() - LAST_BATCH_SENT).seconds >= 120
    ):
        send_batch_to_hec(event_queue, target, token, index, sourcetype)
        event_queue = []  # Clear the queue after sending
        LAST_BATCH_SENT = datetime.now()


def send_batch_to_hec(events, target, token, index, sourcetype):
    url = f"https://{target}/services/collector"
    headers = {"Authorization": f"Splunk {token}"}
    data = "".join(
        [
            json.dumps(
                {
                    "event": event,
                    "index": index,
                    "sourcetype": sourcetype,
                    "source": "ml_gen.py",
                }
            )
            for event in events
        ]
    )
    requests.post(url, headers=headers, data=data, verify=False)


def backfill_metrics(variation_pct, ref_sample, days, target, token, index, sourcetype):
    now = datetime.now()
    past = now - timedelta(days=days)
    delta = timedelta(minutes=1)  # Adjusted to 1 minute interval for backfill

    current_time = past
    while current_time <= now:
        # Pass 0 for variation_pct so that backfill metrics are not influenced by the variation
        metric = generate_metric_for_time(current_time, 0, ref_sample, mode="curve")
        add_event_to_queue(metric, target, token, index, sourcetype)
        current_time += delta


def main():
    parser = argparse.ArgumentParser(
        description="Event generator script that can write events to a local file or send them to Splunk HEC."
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="curve",
        choices=["curve", "stdev"],
        help="Mode to determine hourly multipliers. Choices are 'curve' and 'stdev'. Default is 'curve'.",
    )
    parser.add_argument(
        "--backfill",
        type=str,
        default="false",
        choices=["true", "false"],
        help='Enable backfilling for 90 days when set to "true". Default is "false".',
    )
    parser.add_argument(
        "--variation_pct",
        type=int,
        default=0,
        choices=range(-100, 101),
        help="Percentage variation to apply to the metrics. Can be between -100 and 100. Default is 0.",
    )
    parser.add_argument(
        "--ref_sample",
        type=str,
        default="v1.0.0",
        help='Reference sample to include in the metric. Default is "v1.0.0".',
    )
    parser.add_argument(
        "--write_local",
        type=str,
        default="true",
        choices=["true", "false"],
        help='Choose to write events to a local file. Default is "true".',
    )
    parser.add_argument(
        "--send_hec",
        type=str,
        default="false",
        choices=["true", "false"],
        help='Choose to send events to Splunk HEC. Default is "false".',
    )
    parser.add_argument(
        "--index",
        type=str,
        default="main",
        help='The name of the index for Splunk HEC. Default is "main".',
    )
    parser.add_argument(
        "--sourcetype",
        type=str,
        default="_json",
        help='The sourcetype for Splunk HEC. Default is "_json".',
    )
    parser.add_argument(
        "--target",
        type=str,
        default="localhost:8088",
        help='The target endpoint for Splunk HEC. Default is "localhost:8088".',
    )
    parser.add_argument(
        "--token",
        type=str,
        default="",
        help="The authentication token for Splunk HEC. No default value.",
    )
    args = parser.parse_args()

    if args.backfill == "true":
        backfill_metrics(
            args.variation_pct,
            args.ref_sample,
            90,
            args.target,
            args.token,
            args.index,
            args.sourcetype,
        )

    while True:
        now = datetime.now()
        metric = generate_metric_for_time(
            now, args.variation_pct, args.ref_sample, args.mode
        )
        if args.write_local == "true":
            with open("ml_event_sampler.log", "a") as log_file:
                log_file.write(json.dumps(metric) + "\n")
        if args.send_hec == "true":
            add_event_to_queue(
                metric, args.target, args.token, args.index, args.sourcetype
            )
        time.sleep(60)  # sleep for 1 minute


if __name__ == "__main__":
    main()
