"""
TrustPulse Governance Evidence Package generator.

DISCLAIMER: This report is a governance decision-support artifact based on
TrustPulse-observed OpenEMR telemetry. It is not a legal determination of
HIPAA compliance, breach status, or notification obligation. Final
determinations require authorized privacy/compliance review.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime
from html import escape
from typing import List, Optional

CLINIC_NAME = os.environ.get("CLINIC_NAME", "Demo Clinic")
_TOKEN_SECRET = os.environ.get("TRUSTPULSE_PATIENT_TOKEN_SECRET", "")


def tokenize_patient_id(patient_id: Optional[str]) -> str:
    """HMAC-SHA256 of patient_id with TRUSTPULSE_PATIENT_TOKEN_SECRET."""
    if not patient_id:
        return "-"
    if _TOKEN_SECRET:
        tok = hmac.new(_TOKEN_SECRET.encode(), patient_id.encode(), hashlib.sha256).hexdigest()[:16]
    else:
        tok = hashlib.sha256(patient_id.encode()).hexdigest()[:16]
    return f"PT-{tok}"


def _safe(v) -> str:
    return escape(str(v)) if v is not None else "-"


def generate_evidence_html(
    case,
    events: list,
    actions: list = None,
    assessment=None,
    reviewer: str = "",
    reviewer_role: str = "",
    manifest=None,       # kept for backwards compatibility (single manifest)
    manifests=None,      # preferred: list of IngestionManifest objects
    is_demo: bool = False,
) -> str:
    now       = datetime.utcnow()
    report_id = f"{now.strftime('%Y%m%d')}-{case.case_id[:8].upper()}"

    # Resolve manifests list; prefer the explicit list, fall back to single object
    _manifests = manifests if manifests else ([manifest] if manifest else [])
    primary    = _manifests[0] if _manifests else None
    manifest_hash  = primary.manifest_hash if primary else None
    source_hash    = primary.source_batch_sha256 if primary else None
    ingested_count = primary.inserted_count if primary else None

    _actions      = actions if actions is not None else []
    action_hashes = [a.record_hash for a in _actions if a.record_hash]

    # Strong evidence hash covering all provenance fields
    evidence_fields = {
        "report_id":       report_id,
        "case_id":         case.case_id,
        "case_status":     case.status,
        "source_log_ids":  sorted([str(e.source_log_id) for e in events]),
        "event_ids":       sorted([e.id for e in events]),
        "event_scores":    [e.risk_score for e in events],
        "event_rule_ids":  [
            [r.get("rule_id") for r in (e.triggered_rules or [])]
            for e in events
        ],
        "manifest_hash":       manifest_hash or "",
        "source_batch_hash":   source_hash or "",
        "case_action_hashes":  action_hashes,
        "reviewer":            reviewer,
        "generated_at":        now.isoformat(),
    }
    evidence_manifest_hash = hashlib.sha256(
        json.dumps(evidence_fields, sort_keys=True, default=str).encode()
    ).hexdigest()

    # Collect unique fired rules across events
    rule_map: dict = {}
    not_evaluated_rules: list = []
    for ev in events:
        for r in (ev.triggered_rules or []):
            rid = r.get("rule_id", "")
            if r.get("not_evaluated") and rid not in rule_map:
                not_evaluated_rules.append(r)
            elif r.get("fired", True) and rid not in rule_map:
                rule_map[rid] = r
    top_rules = sorted(rule_map.values(),
                       key=lambda r: r.get("score_contribution", 0), reverse=True)[:5]

    demo_banner = (
        '<div style="background:#FFF3CD;border:2px solid #856404;padding:12px 16px;'
        'margin-bottom:16px;border-radius:4px;">'
        '<b>DEMO SCENARIO REVIEW</b> - This case was generated from demo activity '
        'created inside a lab OpenEMR instance. Logs originate from OpenEMR itself; '
        'no log rows were fabricated by TrustPulse.'
        '</div>'
    ) if is_demo else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TrustPulse Governance Evidence Package - {_safe(report_id)}</title>
<style>
  body {{ font-family:'Courier New',monospace; max-width:960px; margin:40px auto;
          padding:0 20px; color:#111; font-size:13px; }}
  h1 {{ font-size:16px; letter-spacing:2px; border-bottom:2px solid #0A2540;
        padding-bottom:6px; }}
  .section-title {{ background:#0A2540; color:white; padding:4px 10px;
                    font-size:13px; letter-spacing:1px; margin:24px 0 8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; }}
  td,th {{ border:1px solid #ccc; padding:5px 8px; text-align:left; }}
  th {{ background:#f0f0f0; font-weight:bold; }}
  .rule-block {{ background:#fef2f2; border-left:3px solid #c00;
                 padding:6px 10px; margin:4px 0; }}
  .ne-block  {{ background:#f0f7ff; border-left:3px solid #4299e1;
                padding:6px 10px; margin:4px 0; }}
  .rule-id   {{ font-weight:bold; color:#c00; }}
  .ne-id     {{ font-weight:bold; color:#2b6cb0; }}
  .meta      {{ font-size:11px; color:#666; }}
  .breach    {{ background:#fff3cd; border:1px solid #f59e0b;
                padding:8px 12px; margin:8px 0; }}
  .low-risk  {{ background:#d1fae5; border:1px solid #059669;
                padding:8px 12px; margin:8px 0; }}
  .disclaimer {{ background:#f0f4ff; border:1px solid #667eea;
                  padding:10px 14px; margin:12px 0; font-size:12px; }}
  pre {{ background:#f8f8f8; padding:8px; border-radius:3px;
         white-space:pre-wrap; font-size:11px; }}
</style>
</head>
<body>
<h1>TRUSTPULSE GOVERNANCE EVIDENCE PACKAGE</h1>

{demo_banner}

<div class="disclaimer">
  <b>IMPORTANT DISCLAIMER:</b> This report is a governance decision-support artifact
  based on TrustPulse-observed OpenEMR telemetry. It is not a legal determination of
  HIPAA compliance, breach status, or notification obligation. Final determinations
  require authorized privacy/compliance review by qualified legal and compliance
  personnel.
</div>

<p class="meta">
  Report ID: TP-{_safe(report_id)}<br>
  Generated: {_safe(now.strftime('%B %d, %Y %H:%M UTC'))}<br>
  Facility: {_safe(CLINIC_NAME)}<br>
  Reviewed by: {_safe(reviewer)} ({_safe(reviewer_role)})<br>
  Review type: {"DEMO SCENARIO" if is_demo else "PRODUCTION"}<br>
  TrustPulse Version: 0.3.0
</p>

<div class="section-title">SECTION 1 - INCIDENT SUMMARY</div>
<table>
  <tr><th>Case Title</th><td>{_safe(case.title)}</td></tr>
  <tr><th>Case ID</th><td>{_safe(case.case_id)}</td></tr>
  <tr><th>Severity</th><td>{_safe(case.severity)}</td></tr>
  <tr><th>Pattern Type</th><td>{_safe(case.pattern_type)}</td></tr>
  <tr><th>Detection Date</th><td>{_safe(case.created_at)}</td></tr>
  <tr><th>Incident Period</th><td>{_safe(case.date_start)} → {_safe(case.date_end)}</td></tr>
  <tr><th>User</th><td>{_safe(case.user_name or case.user_id)}</td></tr>
  <tr><th>Total Events</th><td>{_safe(case.event_count)}</td></tr>
  <tr><th>Case Status</th><td>{_safe(case.status)}</td></tr>
  <tr><th>Breach Risk Flag</th><td>{_safe('YES' if case.breach_risk else 'NO')}</td></tr>
  <tr><th>Recommended Action</th><td>{_safe(case.recommended_action)}</td></tr>
</table>
<p><b>HIPAA Provisions Implicated:</b>
  {''.join(f'&nbsp;• {_safe(p)}' for p in (case.hipaa_provisions or []))}
</p>

<div class="section-title">SECTION 2 - EVIDENCE RECORD ({len(events)} events from real OpenEMR logs)</div>
<table>
  <tr><th>#</th><th>Source Log ID</th><th>Timestamp</th><th>User</th><th>Event Type</th>
      <th>Patient Token</th><th>IP Address</th><th>Dept</th><th>Risk Score</th></tr>
  {''.join(
      f'<tr><td>{i+1}</td>'
      f'<td class="meta">{_safe(e.source_log_id)}</td>'
      f'<td>{_safe(e.event_time)}</td>'
      f'<td>{_safe(e.user_id)}</td>'
      f'<td>{_safe(e.event_type)}</td>'
      f'<td class="meta">{tokenize_patient_id(e.patient_id)}</td>'
      f'<td>{_safe(e.ip_address)}</td>'
      f'<td>{_safe(e.department)}</td>'
      f'<td>{e.risk_score:.1f}</td></tr>'
      for i, e in enumerate(events)
  )}
</table>
<p class="meta">Patient IDs are tokenized using HMAC-SHA256 and are not reversible
without the TRUSTPULSE_PATIENT_TOKEN_SECRET held by the TrustPulse operator.</p>

<div class="section-title">SECTION 3 - RISK ANALYSIS</div>
<p><b>Composite Risk Score: {case.risk_score:.1f}/100 - {_safe(case.severity)}</b></p>
{''.join(
    f'<div class="rule-block">'
    f'<span class="rule-id">[{_safe(r.get("rule_id"))}] {_safe(r.get("rule_name"))}</span><br>'
    f'{_safe(r.get("description"))} (+{r.get("score_contribution",0)} pts)<br>'
    f'<span class="meta">{_safe(r.get("hipaa_ref"))} | '
    f'Confidence: {_safe(r.get("confidence","?"))}</span>'
    f'{"".join(f"<br><span class=meta>⚠ {_safe(lim)}</span>" for lim in r.get("limitations",[]))}'
    f'</div>'
    for r in top_rules
) if top_rules else "<p>No rules fired.</p>"}

{'<p><b>Rules not evaluated (missing context):</b></p>' + "".join(
    f'<div class="ne-block">'
    f'<span class="ne-id">[{_safe(r.get("rule_id"))}]</span> {_safe(r.get("rule_name"))}<br>'
    f'<span class="meta">Not evaluated: {_safe(r.get("not_evaluated_reason"))}</span>'
    f'</div>'
    for r in not_evaluated_rules
) if not_evaluated_rules else ""}

{_render_assessment_section(assessment) if assessment else ""}

<div class="section-title">SECTION 5 - DISPOSITION & CHAIN OF CUSTODY</div>
<table>
  <tr><th>Case Status</th><td>{_safe(case.status)}</td></tr>
  <tr><th>Resolved At</th><td>{_safe(case.resolved_at) if case.resolved_at else '-'}</td></tr>
</table>

<div class="section-title">SECTION 5A - HUMAN REVIEW ACTION HISTORY</div>
{'<table><tr><th>Timestamp (UTC)</th><th>Reviewer</th><th>Role</th><th>Action</th>'
 '<th>Previous Status</th><th>New Status</th><th>Notes / Reason</th><th>Record Hash (SHA-256, first 16)</th></tr>'
 + ''.join(
   f'<tr>'
   f'<td class="meta">{_safe(a.created_at)}</td>'
   f'<td>{_safe(a.actor_email)}</td>'
   f'<td>{_safe(a.actor_role)}</td>'
   f'<td><b>{_safe(a.action)}</b></td>'
   f'<td>{_safe(a.previous_status)}</td>'
   f'<td>{_safe(a.new_status)}</td>'
   f'<td>{_safe((a.notes or "") + (" | " + a.reason_code if a.reason_code else ""))}</td>'
   f'<td class="meta">{_safe(a.record_hash[:16] if a.record_hash else "-")}…</td>'
   f'</tr>'
   for a in _actions
 )
 + '</table>'
 if _actions else '<p class="meta">No disposition actions recorded for this case.</p>'}
<p class="meta">Each record hash is SHA-256 over {"{action, reviewer, role, status, notes, timestamp, previous_hash}"}
chained to the previous action - modification of any entry breaks the chain.</p>

<div class="section-title">SECTION 6 - TELEMETRY INTEGRITY & PROVENANCE</div>
<pre>
Source System:              OpenEMR (read-only connector)
Connector:                  openemr_real
Evidence Manifest Hash:     {evidence_manifest_hash}
Report ID:                  TP-{report_id}
Generated At:               {now.strftime('%Y-%m-%dT%H:%M:%SZ')}
Exported By:                {reviewer} ({reviewer_role})
OpenEMR Writeback:          DISABLED - TrustPulse operates in read-only mode
</pre>
<p><b>Source Ingestion Manifest(s) for case events:</b></p>
<table>
  <tr><th>#</th><th>Manifest ID</th><th>Started At</th><th>Events Ingested</th>
      <th>Source Batch SHA-256</th><th>Manifest Hash (SHA-256)</th></tr>
  {''.join(
      f'<tr><td>{i+1}</td>'
      f'<td>{_safe(m.id)}</td>'
      f'<td class="meta">{_safe(m.started_at)}</td>'
      f'<td>{_safe(m.inserted_count)}</td>'
      f'<td class="meta">{_safe(m.source_batch_sha256 or "N/A")}</td>'
      f'<td class="meta">{_safe(m.manifest_hash or "N/A")}</td>'
      f'</tr>'
      for i, m in enumerate(_manifests)
  ) if _manifests else "<tr><td colspan=6>No manifest data - events pre-date manifest tracking.</td></tr>"}
</table>
<pre>
LIMITATIONS:
- This evidence package reflects TrustPulse-observed OpenEMR telemetry only.
- Not all OpenEMR actions may be logged in the accessible audit tables.
- Patient ID tokenization uses HMAC-SHA256; tokens are not reversible without
  the operator-held TRUSTPULSE_PATIENT_TOKEN_SECRET.
- This report does not constitute a final HIPAA compliance determination,
  breach notification, or legal finding.
- Final breach-notification determinations require qualified legal and
  compliance review under 45 CFR §164.402–414.

HIPAA Reference: 45 CFR §164.312(b) - Audit Controls
</pre>
</body></html>"""


def _render_assessment_section(assessment) -> str:
    det = assessment.determination
    if det in ("HIGH_RISK", "BREACH"):
        risk_box = (
            '<div class="breach">&#9888; <b>Potential breach-risk condition identified for privacy officer review.</b><br>'
            f'Assessed risk period ends: {_safe(assessment.ocr_deadline)}<br>'
            '<i>Notification obligations require authorized compliance/legal determination. '
            'This report does not constitute a legal finding. Consult your Privacy Officer before taking any notification action.</i></div>'
        )
    else:
        risk_box = (
            '<div class="low-risk">&#10003; <b>Low-risk determination - no immediate notification indicated.</b> '
            'Document and retain for 6 years per §164.530(j). '
            'Final determination requires authorized compliance review.</div>'
        )

    return f"""
<div class="section-title">SECTION 4 - BREACH RISK ASSESSMENT (45 CFR §164.402)</div>
<table>
  <tr><th>Q1 - Unauthorized access?</th><td>{_safe(assessment.q1_unauthorized)}</td></tr>
  <tr><th>Q2 - PHI acquired/viewed?</th><td>{_safe(assessment.q2_acquired)}</td></tr>
  <tr><th>Q3 - Further disclosure?</th><td>{_safe(assessment.q3_disclosed)}</td></tr>
  <tr><th>Factor 1 - Nature/extent of PHI</th><td>{_safe(assessment.factor1_score)}/5</td></tr>
  <tr><th>Factor 2 - Who accessed</th><td>{_safe(assessment.factor2_score)}/5</td></tr>
  <tr><th>Factor 4 - Risk mitigated</th><td>{_safe('YES' if assessment.factor4_mitigated else 'NO')}</td></tr>
  <tr><th>Determination</th><td><b>{_safe(det)}</b></td></tr>
  <tr><th>Assessment by</th><td>{_safe(assessment.completed_by)} on {_safe(assessment.completed_at)}</td></tr>
</table>
{risk_box}"""
