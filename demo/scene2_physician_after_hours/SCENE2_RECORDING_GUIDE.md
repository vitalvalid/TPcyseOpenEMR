# Scene 2 – Physician After-Hours Patient Access: Recording Guide

**Duration:** ~8 minutes  
**Persona:** Compliance Officer / Privacy Reviewer evaluating a TrustPulse after-hours alert

---

## Pre-flight (run before recording)

```bash
cd /home/sagarbh/Desktop/cyseOpenEMR

# 1. Wipe previous Scene 2 data
python demo/scene2_physician_after_hours/reset_scene2.py

# 2. Seed OpenEMR with dr_nguyen after-hours activity
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true \
  python demo/scene2_physician_after_hours/seed_scene2_physician_after_hours.py

# 3. Run ingestion and case generation
bash demo/scene2_physician_after_hours/run_scene2_ingestion.sh

# 4. Verify everything is correct
python demo/scene2_physician_after_hours/verify_scene2.py
```

All checks must show `[PASS]` before you start recording.

---

## Step-by-Step Recording Script

### Step 1 – Open TrustPulse (00:00–00:30)

1. Open browser, navigate to `http://localhost:8000`
2. Log in as `admin@trustpulse.local` / `TrustPulse@2026!`
3. You land on the **Dashboard**

**Narration:**  
*"TrustPulse continuously monitors OpenEMR audit logs and surfaces patterns that warrant human review. Let's walk through a realistic scenario from this past Saturday evening."*

---

### Step 2 – Dashboard overview (00:30–01:00)

1. Point to the **Open Cases** count — at least 1 new case visible
2. Note the `P2_MEDIUM` severity badge on the dr_nguyen case
3. Note the case title: **After-Hours Patient Access – Michael Nguyen**

**Narration:**  
*"TrustPulse has flagged a medium-severity case for Dr. Michael Nguyen, a physician in Internal Medicine. It was triggered by patient-record accesses late on a Saturday evening."*

---

### Step 3 – Open the case (01:00–01:45)

1. Click the dr_nguyen case card
2. Case drawer / detail page opens
3. Highlight the case header fields:
   - Case type: **PATIENT_ACCESS_REVIEW**
   - Pattern: **OFF_HOURS**
   - Severity: **P2 Medium (score 35.0)**
   - Time window: **2026-05-02 21:05 – 21:21 UTC (Saturday)**
   - Event count: **5**

**Narration:**  
*"The case type is Patient Access Review with an Off-Hours pattern. Dr. Nguyen accessed five patient records between 9:05 and 9:21 PM on a Saturday — outside normal clinic hours. TrustPulse scored this at 35 points: 20 for after-hours access, 15 for weekend access."*

---

### Step 4 – Review the evidence tab (01:45–02:30)

1. Click the **Evidence** tab (or scroll to the event list)
2. Confirm exactly **5 events**, all of type `patient_access`
3. Point out patient identifiers shown as `PT-xxxxxxxxxxxxxxxx` tokens — no raw patient IDs
4. Note timestamps spaced a few minutes apart across the Saturday evening window

**Narration:**  
*"Five patient-record accesses in sixteen minutes. Every event is linked here with a tokenized patient identifier — TrustPulse never exposes raw patient IDs. No failed login events are present, which is consistent with legitimate credential use."*

---

### Step 5 – Open the Quick Review Guide (02:30–03:00)

1. Click **Quick Review Guide** (or the assessment / checklist panel)
2. Show the `CLINICIAN_ACCESS_REVIEW` template with three checklist items:
   - Treatment, on-call, or care-coordination reason
   - Patients are scheduled, admitted, or otherwise assigned to this physician
   - Timing is consistent with a clinical workflow

**Narration:**  
*"TrustPulse adapts the review checklist to the actor's role. For a physician, the questions are clinical: was this treatment, on-call coverage, or care coordination? Are these patients assigned to Dr. Nguyen? Note that there is no billing language here — TrustPulse keeps the review context role-specific."*

---

### Step 6 – Send a context request (03:00–04:00)

1. Click **Request Context** (or the context request action)
2. The dialog pre-fills with the `REQUEST_TREATMENT_OR_ON_CALL_CONTEXT` template:
   - **Requested from role:** Provider / Physician
   - **Requested from email:** `dr-nguyen@clinic.local`
   - **Question:** *"Please confirm whether this access was related to treatment, on-call coverage, urgent follow-up, or care coordination."*
3. Add reviewer notes:  
   *"Requesting clinical context before closing this after-hours patient-access review."*
4. Click **Send** / **Submit**

**Narration:**  
*"Rather than escalating immediately, the reviewer sends a context request directly to Dr. Nguyen. This is the right first step — after-hours physician access is common and often entirely legitimate. The request is logged in the case audit trail."*

---

### Step 7 – Simulate an escalation path (04:00–05:00)

> Skip this step if demonstrating the resolved / legitimate path.

1. Assume context was not provided or was insufficient
2. Click **Escalate**
3. Fill in:
   - **Escalated to role:** Privacy Officer
   - **Escalated to email:** `privacy-officer@clinic.local`
   - **Escalation reason:** *"Clinical context could not be confirmed for after-hours patient-record access."*
4. Submit the escalation

**Narration:**  
*"If Dr. Nguyen does not respond or context cannot be confirmed, the case escalates to the Privacy Officer. The escalation is timestamped and attributable — no manual notes or side emails needed."*

---

### Step 8 – Show what TrustPulse does NOT assert (05:00–05:30)

1. Point to the **Breach Risk** field: `false`
2. Point to the case status: `OPEN` (not CONFIRMED_VIOLATION)
3. Highlight the disclaimer in the case or evidence panel

**Narration:**  
*"Critically — TrustPulse has not labelled this a breach. The breach-risk flag is false. The Off-Hours pattern triggers review, not automatic disposition. This is by design: a physician accessing records at 9 PM on a Saturday may be entirely appropriate. The system surfaces the pattern; a human makes the call."*

---

### Step 9 – Export the evidence package (05:30–06:30)

1. Click **Export Evidence** (the export / download button)
2. Browser downloads `trustpulse_evidence_XXXXXXXX.html`
3. Open the downloaded file in a new tab
4. Point out the following sections:
   - Case header: severity P2 Medium, pattern OFF_HOURS, 5 events
   - All 5 events with `PT-` tokens — no raw patient IDs
   - Triggered rules: R-01 After-Hours (20 pts), R-03 Weekend (15 pts) — total 35
   - Context request: question text + pending/responded status
   - Escalation (if taken): role, email, reason, timestamp
   - Review action history with hash chain
   - Limitations / human-review disclaimer section

**Narration:**  
*"The evidence package is a single self-contained HTML file. It captures the full audit trail — the five events, the scoring rules that fired, the context request, and any escalation — without exposing any raw patient identifiers. This file is suitable for a compliance review, a privacy investigation, or legal hold."*

---

### Step 10 – Telemetry health (06:30–07:00)

1. Navigate to **Admin → System Health** or the **Ingestion** tab
2. Show ingestion status: `HEALTHY`
3. Note any missing-context fields reported (appointment data, IP address):
   - These appear as `None` or `MISSING` — expected for this scene
4. Show the manifest hash chain and last ingestion timestamp

**Narration:**  
*"Telemetry shows the ingestion is healthy. You may notice that appointment context and IP address are flagged as unavailable — TrustPulse is honest about what it could not verify. This does not invalidate the case; it informs the reviewer that additional manual checks may be warranted."*

---

### Step 11 – Closing summary (07:00–08:00)

Return to the dashboard and summarise:

*"In this demo we saw TrustPulse:"*
1. *Automatically detect a physician accessing 5 patient records after hours on a Saturday evening*
2. *Fire two scoring rules — After-Hours Access (20 pts) and Weekend Access (15 pts) — producing a P2 Medium case*
3. *Provide a clinician-specific review checklist — no billing language, no false escalation*
4. *Allow the reviewer to send a structured context request directly from the case*
5. *Escalate to a Privacy Officer when context could not be confirmed*
6. *Export a tamper-evident HTML evidence package with full audit trail and no raw patient IDs*
7. *Correctly report breach risk as false — this is human review, not automatic accusation*

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No case appears after ingestion | Re-run `run_scene2_ingestion.sh` — check terminal for 0-event warnings |
| `verify_scene2.py` reports FAIL | Run `reset_scene2.py`, re-seed, re-ingest |
| Score is 20 instead of 35 | Seed date was not a Saturday — re-seed with the correct date (2026-05-02) |
| Breach risk shows `true` | Unexpected — confirm case type is `PATIENT_ACCESS_REVIEW` with `OFF_HOURS` pattern |
| Evidence export returns 403 | Ensure logged in as admin or a role with export permission |
| Appointment context shows as missing | Expected behaviour — `postcalendar_events` is not populated in this demo |
| `requests` module not found | `pip install requests` in your local Python environment |

---

## Reset Between Takes

```bash
python demo/scene2_physician_after_hours/reset_scene2.py
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true \
  python demo/scene2_physician_after_hours/seed_scene2_physician_after_hours.py
bash demo/scene2_physician_after_hours/run_scene2_ingestion.sh
python demo/scene2_physician_after_hours/verify_scene2.py
```
