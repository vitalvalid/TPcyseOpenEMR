#!/usr/bin/env bash
# Scene 2 – Physician After-Hours Patient Access
# One-shot setup: reset → seed → ingest → summary
#
# USAGE
#   ./setup_scene2.sh
#
# ENV VARS (optional overrides)
#   TRUSTPULSE_CONTAINER  (default: trustpulse_app)
#   OPENEMR_CONTAINER     (default: openemr_mariadb)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TP_CONTAINER="${TRUSTPULSE_CONTAINER:-trustpulse_app}"

echo "===================================================="
echo "  Scene 2 Setup  (reset → seed → ingest)"
echo "  Physician After-Hours Patient Access"
echo "===================================================="
echo ""

echo "[1/3] Resetting previous Scene 2 data..."
python3 "${SCRIPT_DIR}/reset_scene2.py"
echo ""

echo "[2/3] Seeding OpenEMR activity..."
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python3 "${SCRIPT_DIR}/seed_scene2_physician_after_hours.py"
echo ""

echo "[3/3] Running ingestion and case generation..."
"${SCRIPT_DIR}/run_scene2_ingestion.sh"
echo ""

echo "===================================================="
echo "  Scene 2 ready.  Run verify_scene2.py to confirm."
echo "===================================================="
