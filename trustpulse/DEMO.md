# TrustPulse Demo Guide

TrustPulse is an OpenEMR-specific compliance-review workflow for small to midsize clinics and hospitals. It ingests OpenEMR-derived audit events, creates patient-access and authentication review cases, and supports human documentation, context collection, escalation, and evidence export.

TrustPulse is not a SIEM replacement, not an automatic HIPAA violation detector, and not a breach-determination engine. The demo should consistently position it as review support for compliance, privacy, billing, and clinic operations teams.

## Demo Reset

Warning: demo reset deletes local TrustPulse and OpenEMR data volumes.

From the repo root:

```bash
docker compose down -v
docker compose -f trustpulse/docker-compose.yml down -v
./setup.sh
```

After startup:

1. Open OpenEMR at `http://localhost:8080`
2. Open TrustPulse at `http://localhost:8000`
3. Log into TrustPulse as `compliance@trustpulse.local / Comply@2026!`
4. Run ingestion once if needed

Use a reset whenever older misgenerated cases remain in the queue after detection or UI changes.

## Demo Credentials

### OpenEMR

- `dr_nguyen / Doctor@2026`
- `dr_patel / Doctor@2026`
- `nurse_chen / Doctor@2026`
- `billing_ross / Doctor@2026`
- `admin_hayes / Doctor@2026`

### TrustPulse

- `admin@trustpulse.local / TrustPulse@2026!`
- `compliance@trustpulse.local / Comply@2026!`
- `auditor@trustpulse.local / Audit@2026!`
- `security@trustpulse.local / Secure@2026!`

## Product Walkthrough

1. Open `Cases`
2. Show recent cases at the top of the queue
3. Open a case drawer
4. Explain `Why this needs review`
5. Open `Quick Review Guide`
6. Use `Use recommended action`
7. Show the prepared `Take Review Action` form
8. Submit a context request or escalation
9. Show `Review Action History`
10. Export the evidence package

## Scenario 1: Billing Volume Spike

User:
- `David Ross / billing_ross / Billing`

What to show:
- `Access Volume Spike - David Ross`
- `Patient access review`
- billing-language `Quick Review Guide`
- `Things to confirm` from the backend billing template
- `Request context` action prepared for `Billing Supervisor`
- context request question asking for billing, payment, claims, insurance, or healthcare-operations justification
- final response recorded in `Context`
- evidence package showing linked evidence, review action history, context request, response, telemetry health, and reason code

Suggested narration:
- “TrustPulse ingested OpenEMR-derived audit events for this billing user.”
- “The linked evidence shows the exact source rows used for the case.”
- “TrustPulse suggests a billing-specific context request, but the reviewer can edit it before sending.”

## Scenario 2: Physician Access Review

User:
- `Michael Nguyen` or `Priya Patel`

What to show:
- `Patient access review`, `After-hours patient access`, or `Volume spike`
- clinician-language `Quick Review Guide`
- backend `Things to confirm` focused on treatment, on-call, care coordination, assigned patients, and timing
- `Request context` action
- optional escalation to `Privacy Officer` if context is not confirmed
- coherent evidence export

Suggested narration:
- “TrustPulse does not make an automatic finding of inappropriate access.”
- “It gives the reviewer a structured path: what happened, what to confirm, and what action to document.”

## Scenario 3: Authentication Review

User:
- `Susan Hayes` or `Linda Chen`

What to show:
- `Failed Login Activity - <User>` for 3 to 4 failed logins within 5 minutes
- `Failed Login Burst - <User>` for 5 or more failed logins within 10 minutes
- `Authentication review` or `Account access review`
- `After-hours` as a secondary chip, not the primary title, for repeated after-hours clusters
- account/security wording in `Quick Review Guide`
- `Request account confirmation` as the suggested next step
- linked evidence limited to the correlated cluster

Suggested narration:
- “One or two failed logins are not turned into a medium or high review case.”
- “Repeated failed logins are clustered in a short, defensible window.”
- “TrustPulse keeps authentication review separate from patient-access review unless the event sequence justifies linking them.”

## What To Click

1. `Cases`
2. Click a visible case row
3. In the drawer, read `Why this needs review`
4. Click `Open Review Guide`
5. Click `Use recommended action`
6. Confirm the action is prepared in `Take Review Action`
7. Submit `Request context` or `Escalated`
8. Open `Workflow`
9. Open `Context`
10. Export evidence from `Evidence`

## What To Say

- “TrustPulse is built specifically around OpenEMR-derived audit activity.”
- “Each case is backed by exact linked evidence rows.”
- “The product supports human review, not automatic legal or breach conclusions.”
- “The Quick Review Guide keeps the next step simple while preserving full technical traceability elsewhere.”
- “The evidence package documents the reviewer’s actions, context requests, escalation, and telemetry integrity.”

## Known Limitations

- Demo data quality depends on the current local OpenEMR lab state.
- A clean reset is recommended before formal demo recording.
- The evidence package is review support and not a final legal or HIPAA determination.
