#!/usr/bin/env bash
set -euo pipefail

if [ -z "${SPLUNK_HEC_TOKEN:-}" ] && [ "${DRY_RUN:-false}" != "true" ]; then
    echo "ERROR: SPLUNK_HEC_TOKEN environment variable is required but not set."
    echo "Set SPLUNK_HEC_TOKEN in your .env file, or set DRY_RUN=true for testing."
    exit 1
fi

echo "============================================="
echo " ML Gen — Synthetic Metrics Generator"
echo "============================================="
echo " HEC URL:             ${SPLUNK_HEC_URL:-https://localhost:8088}"
echo " Index:               ${SPLUNK_INDEX:-mlgen}"
echo " Sourcetype:          ${SPLUNK_SOURCETYPE:-_json}"
echo " Mode:                ${MODE:-normal}"
echo " Backfill Days:       ${BACKFILL_DAYS:-90}"
echo " Normal Entities:     ${NUM_NORMAL:-5}"
echo " Lower Outliers:      ${NUM_LOWER_OUTLIER:-1}"
echo " Upper Outliers:      ${NUM_UPPER_OUTLIER:-1}"
echo " Variation:           ${VARIATION_PCT:-75}%"
echo " Entity Prefix:       ${ENTITY_PREFIX:-sample}"
echo " Interval:            ${GENERATION_INTERVAL:-60}s"
echo " Seasonality:         ${SEASONALITY_MODE:-curve}"
echo " SSL Verify:          ${SSL_VERIFY:-false}"
echo " Dry Run:             ${DRY_RUN:-false}"
echo "============================================="

exec python3 /app/ml_gen.py
