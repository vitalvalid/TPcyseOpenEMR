# Scene 1 – Billing High-Volume Access Review: Recording Guide

**Duration:** ~8 minutes  
**Persona:** Compliance Officer / Security Analyst reviewing TrustPulse alerts

---

## Pre-flight (run before recording)

```bash
cd demo/scene1_billing_high_volume

# 1. Wipe previous Scene 1 data
python reset_scene1.py

# 2. Seed OpenEMR with billing_ross activity
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python seed_scene1_openemr_activity.py

# 3. Run ingestion and case generation
./run_scene1_ingestion.sh

# 4. Verify everything is correct
pip install requests 2>/dev/null; python verify_scene1.py
```

All five checks must show `[PASS]` before you start recording.

---

## Step-by-Step Recording Script

### Step 1 – Open TrustPulse (00:00–00:30)

1. Open browser, navigate to `http://localhost:8000`
2. Log in as `admin@trustpulse.local` / `TrustPulse@2026!`
3. You land on the **Dashboard**

**Narration:**  
*"TrustPulse monitors OpenEMR audit logs in real time. Let's look at what triggered today."*

---

### Step 2 – Dashboard overview (00:30–01:00)

1. Point to the **Open Cases** count — at least 1 case visible
2. Note the `P2_MEDIUM` severity badge on the billing_ross case

**Narration:**  
*"The system has automatically flagged a medium-severity case for David Ross in the Billing department."*

---

### Step 3 – Open the case (01:00–01:45)

1. Click the billing_ross case card
2. Case drawer / detail page opens
3. Highlight the case header:
   - Case type: **Patient Access Review**
   - Severity: **P2 Medium**
   - Time window: **2026-05-05 10:00–10:45**
   - Event count: **55**

**Narration:**  
*"In 45 minutes, billing_ross accessed 55 patient records — well above normal. TrustPulse fired two rules: Bulk Patient Access (28 unique patients) and Volume Spike (55 total accesses)."*

---

### Step 4 – Review the event timeline (01:45–02:30)

1. Scroll through the event list in the drawer / events tab
2. Point out timestamps spaced ~50 seconds apart
3. Point out that patient IDs appear as `PT-` tokens — not raw numbers

**Narration:**  
*"Every event is logged with a tokenized patient identifier — privacy is preserved throughout the audit trail."*

---

### Step 5 – Open the Quick Review Guide (02:30–03:00)

1. Click **Quick Review Guide** (or the checklist / assessment panel)
2. Show the BILLING_ACCESS_REVIEW template:
   - Billing, payment, claims, or insurance reason
   - Assigned billing work queue or follow-up list
   - Supervisor confirmation if unclear

**Narration:**  
*"TrustPulse provides a role-specific review checklist. For billing staff, we ask whether this access is tied to a work queue or claims processing task."*

---

### Step 6 – Send a context request (03:00–04:00)

1. Click **Request Context** (or the context request button)
2. The dialog shows the pre-filled recipient (`billing_ross` / `billing_ross@clinic.example`)
3. Enter a message:  
   *"Hi David, we noticed 55 patient record accesses between 10:00 and 10:45 AM on May 5th. Can you confirm what work queue or task this was related to?"*
4. Click **Send** / **Submit**

**Narration:**  
*"Rather than escalating immediately, the reviewer can send a context request directly from the case — keeping the process lightweight and auditable."*

---

### Step 7 – Simulate a context response (04:00–04:30)

1. Click **Respond** on the pending context request
2. Enter a response:  
   *"Hi team — I was working through the May EOB reconciliation backlog assigned by my supervisor. All accesses are documented in the billing system."*
3. Submit the response

**Narration:**  
*"The response is recorded in the case audit trail with a timestamp. This is the complete interaction log — no separate email thread needed."*

---

### Step 8 – Set case disposition (04:30–05:15)

1. Click **Resolve** (or the status/disposition control)
2. Select disposition: **Legitimate Business Activity** (or equivalent)
3. Add a reviewer note:  
   *"Confirmed with billing_ross: EOB reconciliation task assigned by supervisor. No further action required."*
4. Click **Save** / **Confirm**

**Narration:**  
*"The case is resolved with a documented justification. The full decision trail — who reviewed, when, and why — is preserved."*

---

### Step 9 – Export the evidence package (05:15–06:00)

1. Click **Export Evidence** (the export / download button)
2. Browser downloads `trustpulse_evidence_XXXXXXXX.html`
3. Open the downloaded file in a new tab
4. Point out:
   - Case header with severity and timestamp
   - All 55 events listed with `PT-` tokens
   - Triggered rules: R-02 (25 pts), R-08 (20 pts), total 45
   - Context request and response in the audit trail
   - Resolution action and reviewer note
   - Ingestion manifest hash (tamper-evident provenance)

**Narration:**  
*"The evidence package is a single self-contained HTML file suitable for compliance review, HR proceedings, or legal hold. It contains the complete audit trail — including the hash of the source manifest — without exposing any raw patient identifiers."*

---

### Step 10 – Telemetry health (06:00–06:30)

1. Navigate to **Admin → System Health** or **Ingestion** tab
2. Point out the telemetry status: `HEALTHY` or `DEGRADED` (not CRITICAL)
3. Show the manifest hash chain and last ingestion time

**Narration:**  
*"TrustPulse tracks ingestion health continuously. The hash chain proves that every event was ingested in order, without gaps or tampering."*

---

### Step 11 – Show noise isolation (06:30–07:15)

1. Go back to the case list
2. Filter by `frontdesk_kim` or search — confirm no auth case appears at medium/high severity
3. Explain: frontdesk_kim had 2 failed logins — below the 3-event threshold

**Narration:**  
*"Background activity is tracked but only surfaces when thresholds are crossed. Two failed logins don't trigger a review — only three or more do. This reduces alert fatigue."*

---

### Step 12 – Closing summary (07:15–08:00)

Return to the dashboard and summarise:

*"In this demo we saw TrustPulse:"*
1. *Automatically detect a billing staff member accessing 55 patient records in 45 minutes*
2. *Fire two rules — Bulk Patient Access and Volume Spike — producing a P2 Medium case*
3. *Allow the reviewer to request context, receive a response, and resolve with full documentation*
4. *Export a tamper-evident HTML evidence package ready for compliance or legal review*
5. *All patient identifiers protected as `PT-` tokens throughout*

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No case appears after ingestion | Re-run `./run_scene1_ingestion.sh` — check for 0-events warning |
| `verify_scene1.py` reports FAIL on event count | Run `reset_scene1.py` then re-seed and re-ingest |
| Evidence export returns 403 | Ensure logged in as admin or a role with `export` permission |
| Container not found errors | Run `docker compose up -d` from the `trustpulse/` directory |
| `requests` module not found | `pip install requests` in your local Python environment |

---

## Reset Between Takes

```bash
python reset_scene1.py
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python seed_scene1_openemr_activity.py
./run_scene1_ingestion.sh
python verify_scene1.py
```
