#!/usr/bin/env bash
# Scene 1 – Billing High-Volume Access Review
# One-shot setup: reset → seed → ingest → summary
#
# USAGE
#   ./setup_scene1.sh
#
# ENV VARS (optional overrides)
#   TRUSTPULSE_CONTAINER  (default: trustpulse_app)
#   OPENEMR_CONTAINER     (default: openemr_mariadb)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TP_CONTAINER="${TRUSTPULSE_CONTAINER:-trustpulse_app}"

echo "===================================================="
echo "  Scene 1 Setup  (reset → seed → ingest)"
echo "===================================================="
echo ""

echo "[1/3] Resetting previous Scene 1 data..."
python3 "${SCRIPT_DIR}/reset_scene1.py"
echo ""

echo "[2/3] Seeding OpenEMR activity..."
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python3 "${SCRIPT_DIR}/seed_scene1_openemr_activity.py"
echo ""

echo "[3/3] Running ingestion and case generation..."
"${SCRIPT_DIR}/run_scene1_ingestion.sh"
echo ""

echo "===================================================="
echo "  Scene 1 ready.  Run verify_scene1.py to confirm."
echo "===================================================="
