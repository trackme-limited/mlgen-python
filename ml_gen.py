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


def generate_sparse_metric(dt, ref_sample, sparse_interval, mode):
    # Adjust the logic to generate a single sparse data point
    # The main difference will be the generation interval, which is sparse_interval
    # For simplicity, we will use the logic from generate_metric_for_time with some modifications

    weekday = dt.strftime("%A")
    hour = dt.hour
    human_readable_time = dt.strftime("%c")  # Convert to human-readable time

    # Use the same logic for getting ranges and applying multipliers
    # However, ensure that the data point is sparse by considering the sparse interval
    multiplier = hourly_multiplier(hour, weekday, mode=mode)
    original_ranges = get_day_ranges(weekday)

    dcount_lower, dcount_upper = original_ranges["dcount_hosts"]
    events_lower, events_upper = original_ranges["events_count"]

    dcount = random.randint(dcount_lower, dcount_upper) * multiplier
    events = random.randint(events_lower, events_upper) * multiplier

    return {
        "time": int(dt.timestamp()),
        "time_human": human_readable_time,
        "dcount_hosts": int(dcount),
        "events_count": int(events),
        "ref_sample": ref_sample,
    }


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
    human_readable_time = dt.strftime("%c")

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
        "time_human": human_readable_time,
        "dcount_hosts": dcount,
        "events_count": events,
        "ref_sample": ref_sample,
    }


def add_event_to_queue(metric, target, token, index, sourcetype, force_send=False):
    global event_queue
    global LAST_BATCH_SENT
    event_queue.append(metric)

    if (
        force_send
        or len(event_queue) >= MAX_BATCH_SIZE
        or (datetime.now() - LAST_BATCH_SENT).seconds >= 120
    ):
        print(f"Sending batch of {len(event_queue)} events.")
        send_batch_to_hec(event_queue, target, token, index, sourcetype)
        event_queue = []
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
                    "time": event.get("time"),
                }
            )
            for event in events
        ]
    )
    response = requests.post(url, headers=headers, data=data, verify=False)
    print(f"Response from Splunk HEC: {response.status_code}, {response.text}")


def backfill_metrics(
    variation_pct, ref_sample, days, target, token, index, sourcetype, mode, sparse_time
):
    global event_queue  # Add this line
    global LAST_BATCH_SENT  # Add this line to access the global variable

    print(
        f"Starting backfill process in {mode} mode with sparse time {sparse_time} seconds"
    )
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)
    current_time = start_time

    while current_time <= end_time:
        print(f"Generating metric for time: {current_time}")
        metric = (
            generate_sparse_metric(current_time, ref_sample, sparse_time, mode)
            if mode == "sparse"
            else generate_metric_for_time(current_time, 0, ref_sample, mode="curve")
        )
        metric["time"] = int(current_time.timestamp())

        add_event_to_queue(metric, target, token, index, sourcetype, force_send=False)
        current_time += timedelta(seconds=sparse_time)

        # Send batch if conditions are met
        if (
            len(event_queue) >= MAX_BATCH_SIZE
            or (datetime.now() - LAST_BATCH_SENT).seconds >= 120
        ):
            send_batch_to_hec(event_queue, target, token, index, sourcetype)
            event_queue = []
            LAST_BATCH_SENT = datetime.now()

    # Send any remaining events in the queue
    if event_queue:
        send_batch_to_hec(event_queue, target, token, index, sourcetype)
        event_queue = []

    print("Backfill process completed.")


def main():
    parser = argparse.ArgumentParser(
        description="Event generator script that can write events to a local file or send them to Splunk HEC."
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="curve",
        choices=["curve", "stdev", "sparse"],
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
    parser.add_argument(
        "--sparse_time",
        type=int,
        default=14400,  # 4 hours
        help="Time interval in seconds between two measurements in sparse mode. Default is 14400 seconds (4 hours).",
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
            args.mode,
            args.sparse_time,
        )

    interval = 60  # Default interval for regular modes
    if args.mode == "sparse":
        interval = args.sparse_time  # Use sparse interval for sparse mode

    while True:
        now = datetime.now()
        print(f"Generating metric in normal mode for time: {now}")

        if args.mode == "sparse":
            metric = generate_sparse_metric(
                now, args.ref_sample, args.sparse_time, args.mode
            )
        else:
            metric = generate_metric_for_time(
                now, args.variation_pct, args.ref_sample, args.mode
            )

        if args.write_local == "true":
            with open("ml_event_sampler.log", "a") as log_file:
                print(f"Writing metric to local file")
                log_file.write(json.dumps(metric) + "\n")

        if args.send_hec == "true":
            if args.mode == "sparse" and args.backfill == "false":
                print("Sending metric directly to Splunk HEC")
                send_batch_to_hec(
                    [metric], args.target, args.token, args.index, args.sourcetype
                )
            else:
                print(f"Adding metric to queue for sending to HEC")
                add_event_to_queue(
                    metric, args.target, args.token, args.index, args.sourcetype
                )

        time.sleep(interval)


if __name__ == "__main__":
    main()
