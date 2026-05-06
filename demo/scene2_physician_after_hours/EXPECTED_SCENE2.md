# Scene 2 – Physician After-Hours Patient Access
## Expected Case Reference

### Business Story

A physician accesses several patient records after normal clinic hours on a Saturday evening.
This may be legitimate treatment, on-call coverage, urgent follow-up, or care coordination.
TrustPulse should **not** label it as a confirmed violation. It should create a human-review
case, surface clinical-context review guidance, and ask a reviewer to confirm or escalate.

---

### Actor

| Field         | Value                    |
|---------------|--------------------------|
| Username      | `dr_nguyen`              |
| Display name  | Michael Nguyen           |
| Role          | Physician / Clinician    |
| Specialty     | Internal Medicine        |
| TrustPulse role (inferred) | `clinician` |

### Reviewer

TrustPulse Admin or Compliance Officer

---

### Seeded Source Events (OpenEMR `log` table)

| # | Timestamp (UTC)      | Username    | OpenEMR event   | TrustPulse type | Patient slot |
|---|----------------------|-------------|-----------------|-----------------|--------------|
| 1 | 2026-05-02 21:05:00  | dr_nguyen   | patient-record  | patient_access  | P014         |
| 2 | 2026-05-02 21:08:00  | dr_nguyen   | patient-record  | patient_access  | P015         |
| 3 | 2026-05-02 21:12:00  | dr_nguyen   | patient-record  | patient_access  | P016         |
| 4 | 2026-05-02 21:16:00  | dr_nguyen   | patient-record  | patient_access  | P017         |
| 5 | 2026-05-02 21:21:00  | dr_nguyen   | patient-record  | patient_access  | P018         |

Demo marker: `TRUSTPULSE_SCENE2_PHYSICIAN_AFTER_HOURS_2026_05_02`

Patient slots P014–P018 map to the 14th–18th OpenEMR `patient_data.pid` values
(0-indexed positions 13–17, ordered by `pid ASC`). Actual integer PIDs vary by
deployment but are never exposed in TrustPulse — only patient tokens appear.

---

### Expected Scoring (per event)

| Rule | Name              | Condition             | Score |
|------|-------------------|-----------------------|-------|
| R-01 | After-Hours Access | hour=21 ≥ 19         | 20.0  |
| R-03 | Weekend Access     | day_of_week=5 (Sat)  | 15.0  |
| R-02 | Bulk Patient Access | daily_pts=5 < 10    | 0.0 (does not fire) |

**Total per event:** 35.0 → **P2_MEDIUM**

---

### Expected TrustPulse Case

| Field              | Expected value                                    |
|--------------------|---------------------------------------------------|
| Title              | After-Hours Patient Access - Michael Nguyen       |
| Case type          | PATIENT_ACCESS_REVIEW                             |
| Pattern type       | OFF_HOURS                                         |
| Severity           | P2_MEDIUM (score 35.0)                            |
| Status             | OPEN                                              |
| Linked events      | 5                                                 |
| Unique patient tokens | 5                                              |
| Event types        | patient_access only (no failed_login)             |
| Breach risk        | false (OFF_HOURS pattern does not trigger breach) |
| HIPAA provisions   | §164.308(a)(3), §164.312(b)                       |
| Recommended action | FOLLOW_UP                                         |

---

### Expected Quick Review Guide

**Checklist template:** `CLINICIAN_ACCESS_REVIEW`

**Things to confirm:**
1. Treatment, on-call, or care-coordination reason
2. Scheduled, admitted, or assigned patients
3. Timing consistent with clinical workflow

**No billing-specific language should appear.**

---

### Expected Context Request Recommendation

| Field               | Value                                                      |
|---------------------|------------------------------------------------------------|
| Template ID         | REQUEST_TREATMENT_OR_ON_CALL_CONTEXT                      |
| Requested from role | Provider / Physician                                       |
| Question text       | Please confirm whether this access was related to treatment, on-call coverage, urgent follow-up, or care coordination. |

---

### Expected Reviewer Workflow

1. Open the **After-Hours Patient Access – Michael Nguyen** case.
2. Confirm case type = `PATIENT_ACCESS_REVIEW`, pattern = `OFF_HOURS`.
3. Confirm **Evidence** tab shows exactly 5 linked `patient_access` events.
4. Confirm patient tokens appear (format `PT-xxxxxxxxxxxxxxxx`) — no raw PIDs.
5. Open **Quick Review Guide**:
   - Confirm clinical-context language (treatment, on-call, care coordination).
   - Confirm no billing-specific language.
6. Submit **REQUEST_CONTEXT**:
   - `requested_from_role`: Provider / Physician
   - `requested_from_email`: dr-nguyen@clinic.local
   - `context_question`: Please confirm whether this access was related to treatment, on-call coverage, urgent follow-up, or care coordination.
   - `notes`: Requesting clinical context before closing this after-hours patient-access review.
7. If context not confirmed, submit **ESCALATED**:
   - `escalated_to_role`: Privacy Officer
   - `escalated_to_email`: privacy-officer@clinic.local
   - `escalation_reason`: Clinical context could not be confirmed for after-hours patient-record access.
8. Export **Evidence**.
9. Confirm evidence package includes:
   - Case summary
   - Linked OpenEMR-derived evidence (5 patient_access events)
   - Patient tokens (no raw PIDs)
   - Context request (question + status)
   - Escalation ownership (role + email + reason)
   - Review action history (hash chain)
   - Telemetry health
   - Limitations / human-review disclaimer

---

### What TrustPulse Does NOT Assert

- This is **not** a confirmed privacy violation.
- The physician's access may have been entirely appropriate.
- TrustPulse surfaces the pattern for human review, not for automatic disposition.

---

### How to Reset and Re-run

```bash
# Reset (removes all scene 2 data)
python demo/scene2_physician_after_hours/reset_scene2.py

# Seed (inserts 5 OpenEMR source rows)
TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true \
  python demo/scene2_physician_after_hours/seed_scene2_physician_after_hours.py

# Ingest + generate case
bash demo/scene2_physician_after_hours/run_scene2_ingestion.sh

# Verify
python demo/scene2_physician_after_hours/verify_scene2.py
```

---

### Known Limitations

- **Appointment context** is not available in this demo. The connector checks
  `openemr_postcalendar_events` but it is typically unavailable, so
  `has_appointment=None` is reported as missing context. This is correct behavior.
- **IP address** is not captured from `log`-only inserts (no `api_log` join).
  This is surfaced as missing context in the telemetry section.
- **Severity** depends on active scoring rules. If rule weights change, severity
  may shift from P2_MEDIUM to P3_LOW; both are valid for this scene.
- **May 2, 2026 is a Saturday.** If a different demo date (weekday) is used,
  R-03 will not fire and total score = 20.0 → P3_LOW. The case is still created.
