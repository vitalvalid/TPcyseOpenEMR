# TrustPulse

TrustPulse is a healthcare audit governance tool that connects to OpenEMR, ingests audit activity, detects risky access patterns, and creates review cases for compliance workflows.

It does not modify OpenEMR source code. It reads OpenEMR data through a read-only database connector and builds cases inside TrustPulse.

This project is a course project for Cybersecurity Systems Engineering and is intended for lab/demo use only.

## What TrustPulse Does

- Connects to OpenEMR with read-only database access
- Ingests audit activity from OpenEMR log tables
- Normalizes and scores events
- Detects patterns such as:
  - after-hours access
  - access volume spikes
  - failed-login bursts
  - credential risk
  - suspicious patient access behavior
- Generates review cases for compliance staff
- Tracks reviewer actions with tamper-evident hashes
- Supports evidence export and governance reporting

## Quick Start

From the repo root:

```bash
git clone <repo-url>
cd cyseOpenEMR
chmod +x setup.sh
./setup.sh
```

On Windows PowerShell:

```powershell
git clone <repo-url>
cd cyseOpenEMR
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

If either setup script does not work in your environment, follow the manual fallback process in [trustpulse/manual.md](/home/sagarbh/Desktop/cyseOpenEMR/trustpulse/manual.md:1).

Once setup completes, open:

- TrustPulse: `http://localhost:8000`
- OpenEMR: `http://localhost:8080`

## Login Credentials

### OpenEMR

These are the working OpenEMR accounts in this environment:

| Username | Password | Role |
|----------|----------|------|
| admin | pass | System administrator |
| dr_nguyen | Doctor@2026 | Physician |
| dr_patel | Doctor@2026 | Physician |
| nurse_chen | Doctor@2026 | Nurse |
| billing_ross | Doctor@2026 | Billing |
| admin_hayes | Doctor@2026 | Administrative user |

### TrustPulse

| Email | Password | Role |
|-------|----------|------|
| admin@trustpulse.local | TrustPulse@2026! | Admin |
| compliance@trustpulse.local | Comply@2026! | Compliance Officer |
| auditor@trustpulse.local | Audit@2026! | Auditor |
| security@trustpulse.local | Secure@2026! | Security Admin |

## Important Behavior

TrustPulse does not create cases directly from manual data entry in OpenEMR.

Instead, the workflow is:

1. A user logs into OpenEMR
2. That user performs activity that generates audit logs
3. TrustPulse ingests the new OpenEMR audit rows
4. TrustPulse scores the events
5. TrustPulse creates cases from risky patterns

Also note:

- OpenEMR `admin` activity is intentionally skipped by the TrustPulse connector
- To generate new TrustPulse events and cases, use non-admin users such as:
  - `dr_nguyen`
  - `dr_patel`
  - `nurse_chen`
  - `billing_ross`
  - `admin_hayes`

## Generating Activity for TrustPulse

To generate visible TrustPulse events and cases:

1. Log into OpenEMR as `dr_patel / Doctor@2026`
2. Go to `Patient Search -> All Patients`
3. Open several different patient charts
4. Repeat with `dr_nguyen / Doctor@2026` if needed
5. Return to TrustPulse
6. Click `Run Ingestion`

After ingestion, TrustPulse will:
- increase ingested event counts
- store normalized audit events
- create or update cases in the case queue

## Why Event Counts May Not Increase

If TrustPulse ingested event counts do not increase, the usual causes are:

- you are using the OpenEMR `admin` account
- no new OpenEMR audit rows were generated
- only previously ingested rows are being seen
- the OpenEMR login/activity happened with a user that TrustPulse skips

In this environment, `admin` logins and actions will not increase TrustPulse event totals.

## Architecture Summary

TrustPulse includes:

- `backend/` for ingestion, scoring, case generation, APIs, and governance features
- `frontend/` for the dashboard UI
- `sql/` for OpenEMR read-only setup
- `tests/` for backend tests
- `tools/` for demo activity helpers

## Stopping the Environment

From the repo root:

```bash
docker compose down
docker compose -f trustpulse/docker-compose.yml down
```

To also remove stored data:

```bash
docker compose down -v
docker compose -f trustpulse/docker-compose.yml down -v
```

## More Details

See:

- `trustpulse/SETUP.md` for full setup details
- `trustpulse/manual.md` for manual fallback setup on Windows PowerShell and WSL2 Ubuntu
- `trustpulse/DESIGN_DECISIONS.md` for design rationale
