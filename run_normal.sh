#!/bin/bash

if [ -f config ]; then
    source config
else
    echo "Please setup your config first." && exit 1
fi

echo "Generating ref_sample: $ref"

# run
python3 ml_gen.py --verbose true --mode curve --backfill false --variation_pct 0 --ref_sample $ref --write_local false --send_hec true --index $target_idx --sourcetype _json --token $token --target $target_hec
