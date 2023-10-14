# Python ML sample generator

See: https://docs.trackme-solutions.com/

## Introduction

**This Python events generator is designed to:**

- Generate and backfill data in Splunk with two metrics, dcount_hosts and events_count
- Generate metrics with concepts of variations and seasonality for ML Outliers detection purposes
- Allow generating an incident with a lower bound outliers, or upper bound outliers to simulate true conditions of issues affecting an entity
- Validate the detection with TrackMe Outliers detection
- Data is sent to an HEC event endpoint and would be parsed for Splunk indexing (direct to Splunk, or via Cribl Logstream)

**Several simple Shell wrappers will be called:**

- ``run_backfill.sh``: calls the generator to backfill metrics for the past 90 days, do not generate an outlier but continously generate metrics with normal behaviours as long as it runs
- ``run_normal.sh``: calls the generator without any backfill, general normal behaviours as long as it runs
- ``run_gen_lower_outliers.sh``: calls the generator to immediately start generating a lower bound outlier influenced by a variation percentage (-75% in curve mode)
- ``run_gen_upper_outliers.sh``: calls the generator to immediately start generating an upper bound outlier influenced by a variation percentage (+75% in curve mode)

**The sequence is therefore:**

- **Step 1**, call ``run_backfill.sh`` and wait until metrics are generator for the real time now period
- **Step 2**, stop the script, and run any of the 3 scripts corresponding to each scenario
- You can also generate a lower or upper bound outlier, then stop the script and run the ``run_normal.sh`` to simulate the end of an anomaly, and conditions coming back to normal

## Howto

### Setup the config file

Create a copy of "config.template" to "config" and setup your HEC target.

**The file "config" should be in the current directory and would contain:**

    # index target
    export target_idx="mlgen"

    # HEC token
    export token="xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"

    # HEC target
    export target_hec="myhec.mydomain.com:8088"

    # change this value to generate a new entity
    export ref="sample001"

### Ensures Splunk is ready to parse the data

**A typical event will be sent in JSON format as:**

    {
        "time": 1697273003,
        "dcount_hosts": 370,
        "events_count": 2343964,
        "ref_sample": "sample102"
    }

**The Shell script define the sourcetype as _json with the argument:**

    --sourcetype _json

Splunk should be capable of properly parsing the time stamp, and extracting metrics at search time.
