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


def _collect_not_evaluated_rule_summaries(events: list) -> list:
    grouped: dict = {}
    for idx, ev in enumerate(events):
        event_marker = getattr(ev, "id", None)
        if event_marker is None:
            event_marker = getattr(ev, "source_log_id", None)
        if event_marker is None:
            event_marker = idx

        seen_keys = set()
        for r in (ev.triggered_rules or []):
            if not r.get("not_evaluated"):
                continue
            reason_text = r.get("not_evaluated_reason", "") or "Missing evaluation context."
            key = (r.get("rule_id", ""), r.get("rule_name", ""), reason_text)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            summary = grouped.setdefault(key, {
                "rule_id": r.get("rule_id", ""),
                "rule_name": r.get("rule_name", ""),
                "not_evaluated_reason": reason_text,
                "affected_events": set(),
            })
            summary["affected_events"].add(event_marker)

    summaries = []
    for summary in grouped.values():
        summaries.append({
            "rule_id": summary["rule_id"],
            "rule_name": summary["rule_name"],
            "not_evaluated_reason": summary["not_evaluated_reason"],
            "affected_event_count": len(summary["affected_events"]),
        })
    return sorted(
        summaries,
        key=lambda r: (-r["affected_event_count"], r["rule_id"], r["not_evaluated_reason"]),
    )


def _derive_review_metadata(case, actions: list, generated_by: str, generated_by_role: str) -> dict:
    substantive_actions = [
        a for a in (actions or [])
        if getattr(a, "action", None) and getattr(a, "action", None) != "EVIDENCE_EXPORTED"
    ]
    latest_review = max(
        substantive_actions,
        key=lambda a: getattr(a, "created_at", datetime.min),
        default=None,
    )
    current_status = getattr(case, "status", None) or "-"
    return {
        "generated_by": generated_by,
        "generated_by_role": generated_by_role,
        "current_status": current_status,
        "human_review_status": (
            f"Latest reviewer: {getattr(latest_review, 'actor_email', '-')} "
            f"({getattr(latest_review, 'actor_role', '-')})"
            if latest_review else
            "No disposition actions recorded"
        ),
        "latest_review": latest_review,
    }


def _hipaa_label(provision: str) -> str:
    labels = {
        "§164.312(b)": "Audit controls",
        "§164.502(b)": "Minimum necessary standard",
        "§164.308(a)(1)": "Security management process",
        "HIPAA 45 CFR §164.312(a)(2)(i)": "Unique user identification",
        "§164.312(a)(2)(i)": "Unique user identification",
    }
    return labels.get(provision, "Compliance review reference")


def _display_action_label(action: dict | None) -> str:
    if not action:
        return "-"
    if action.get("action_label"):
        return action["action_label"]
    raw = action.get("action") or "Review"
    return str(raw).replace("_", " ").title()


def _auth_linked_sequence(event_types: list[str]) -> str:
    ordered = event_types or []
    if not ordered:
        return "-"
    labels = []
    for event_type in ordered:
        if event_type == "failed_login":
            labels.append("failed login")
        elif event_type in ("successful_login", "login", "authentication_success"):
            labels.append("successful login")
        elif event_type in ("patient_access", "patient_view", "patient_record", "record_modify", "export", "download"):
            labels.append("patient access")
        else:
            labels.append(str(event_type).replace("_", " "))
    return " → ".join(labels)


def generate_evidence_html(
    case,
    events: list,
    actions: list = None,
    context_requests: list = None,
    assessment=None,
    patient_access_assessment=None,
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
    telemetry_label = getattr(primary, "telemetry_status_label", None) if primary else None
    telemetry_desc = getattr(primary, "telemetry_status_description", None) if primary else None

    _actions      = actions if actions is not None else []
    _context_requests = context_requests if context_requests is not None else []
    action_hashes = [a.record_hash for a in _actions if a.record_hash]
    review_meta   = _derive_review_metadata(case, _actions, reviewer, reviewer_role)

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
    not_evaluated_rules = _collect_not_evaluated_rule_summaries(events)
    for ev in events:
        for r in (ev.triggered_rules or []):
            rid = r.get("rule_id", "")
            if not r.get("not_evaluated") and r.get("fired", True) and rid not in rule_map:
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
  <b>IMPORTANT DISCLAIMER:</b> This evidence package supports human compliance/privacy review.
  It is not an automatic finding of inappropriate access, breach, or HIPAA violation.<br><br>
  This report is a governance decision-support artifact
  based on TrustPulse-observed OpenEMR telemetry. It is not a legal determination of
  HIPAA compliance, breach status, or notification obligation. Final determinations
  require authorized privacy/compliance review by qualified legal and compliance personnel.
</div>

<p class="meta">
  Report ID: TP-{_safe(report_id)}<br>
  Generated: {_safe(now.strftime('%B %d, %Y %H:%M UTC'))}<br>
  Facility: {_safe(CLINIC_NAME)}<br>
  Generated by: {_safe(review_meta["generated_by"])} ({_safe(review_meta["generated_by_role"])})<br>
  Current review status: {_safe(review_meta["current_status"])}<br>
  Human review status: {_safe(review_meta["human_review_status"])}<br>
  Review type: {"DEMO SCENARIO" if is_demo else "PRODUCTION"}<br>
  TrustPulse Version: 0.3.0
</p>

<div class="section-title">SECTION 1 - CASE SUMMARY</div>
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
<p class="meta">Provision tags are compliance-review references, not automatic findings of violation.</p>
{('<table><tr><th>Provision Tag</th><th>Reference Label</th></tr>' + ''.join(
    f'<tr><td>{_safe(p)}</td><td>{_safe(_hipaa_label(p))}</td></tr>'
    for p in (case.hipaa_provisions or [])
) + '</table>') if case.hipaa_provisions else ''}

<div class="section-title">SECTION 2 - LINKED OPENEMR-DERIVED EVIDENCE{" (AUTHENTICATION EVENTS)" if getattr(case, "case_type", "") in ("AUTHENTICATION_REVIEW", "ACCOUNT_ACCESS_REVIEW", "ACCOUNT_MISUSE_REVIEW") else ""}</div>
<p><b>{len(events)} linked OpenEMR-derived audit events observed by TrustPulse.</b></p>
<table>
  <tr><th>#</th><th>Source Log ID</th><th>Manifest ID</th><th>Timestamp</th><th>Source Username</th><th>User</th><th>Event Type</th>
      <th>Patient Token</th><th>IP Address</th><th>Dept</th><th>Risk Score</th></tr>
  {''.join(
      f'<tr><td>{i+1}</td>'
      f'<td class="meta">{_safe(e.source_log_id)}</td>'
      f'<td class="meta">{_safe(getattr(e, "manifest_id", None))}</td>'
      f'<td>{_safe(e.event_time)}</td>'
      f'<td>{_safe(getattr(e, "user_name", None) or e.user_id)}</td>'
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
{('<p class="meta"><b>Source provenance:</b> '
   + ' | '.join(
       f'Manifest #{_safe(getattr(m, "id", None))} via {_safe(getattr(m, "connector_name", None))}'
       f' ({_safe(getattr(m, "source_name", None))})'
       f' · manifest hash {_safe((getattr(m, "manifest_hash", "") or "-")[:16])}…'
       for m in manifests
   )
   + '</p>') if manifests else ''}

<div class="section-title">SECTION 3 - RISK ANALYSIS AND RULE EXPLANATIONS</div>
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

{_render_assessment_section(assessment) if assessment else ""}

{_render_review_section(case, patient_access_assessment) if patient_access_assessment else ""}

<div class="section-title">SECTION 4 - MISSING CONTEXT / RULES NOT EVALUATED</div>
{('<p><b>Rules not evaluated due to missing context:</b></p>' + "".join(
    f'<div class="ne-block">'
    f'<span class="ne-id">[{_safe(r.get("rule_id"))}]</span> {_safe(r.get("rule_name"))}<br>'
    f'<span class="meta">Reason: {_safe(r.get("not_evaluated_reason"))}</span><br>'
    f'<span class="meta">Affected linked events: {_safe(r.get("affected_event_count"))}</span>'
    f'</div>'
    for r in not_evaluated_rules
)) if not_evaluated_rules else '<p class="meta">No rule-evaluation gaps were recorded for the linked events in this case.</p>'}

<div class="section-title">SECTION 5 - DISPOSITION AND CHAIN OF CUSTODY</div>
<table>
  <tr><th>Case Status</th><td>{_safe(case.status)}</td></tr>
  <tr><th>Resolved At</th><td>{_safe(case.resolved_at) if case.resolved_at else '-'}</td></tr>
</table>

<div class="section-title">SECTION 5A - HUMAN REVIEW ACTION HISTORY</div>
{'<table><tr><th>Timestamp (UTC)</th><th>Reviewer</th><th>Role</th><th>Action</th>'
 '<th>Previous Status</th><th>New Status</th><th>Reason Code</th><th>Reason Label</th><th>Notes</th><th>Record Hash (SHA-256, first 16)</th></tr>'
 + ''.join(
   f'<tr>'
   f'<td class="meta">{_safe(a.created_at)}</td>'
   f'<td>{_safe(a.actor_email)}</td>'
   f'<td>{_safe(a.actor_role)}</td>'
   f'<td><b>{_safe(a.action)}</b></td>'
   f'<td>{_safe(a.previous_status)}</td>'
   f'<td>{_safe(a.new_status)}</td>'
   f'<td>{_safe(a.reason_code)}</td>'
   f'<td>{_safe(getattr(a, "reason_label_snapshot", None))}</td>'
   f'<td>{_safe(a.notes)}</td>'
   f'<td class="meta">{_safe(a.record_hash[:16] if a.record_hash else "-")}…</td>'
   f'</tr>'
   for a in _actions
 )
 + '</table>'
if _actions else '<p class="meta">No disposition actions recorded for this case.</p>'}
<p class="meta">Each record hash is SHA-256 over {"{action, reviewer, role, status, notes, timestamp, previous_hash}"}
chained to the previous action - modification of any entry breaks the chain.</p>

<div class="section-title">SECTION 5B - CONTEXT REQUESTS</div>
{'<table><tr><th>Requested By</th><th>Requested From</th><th>Question (Final Submitted)</th><th>Due Date</th><th>Status</th><th>Response</th></tr>'
 + ''.join(
   f'<tr>'
   f'<td>{_safe(r.requested_by_email)}</td>'
   f'<td>{_safe(r.requested_from_role)}{" (" + _safe(r.requested_from_email) + ")" if getattr(r, "requested_from_email", None) else ""}</td>'
   f'<td>{_safe(r.question)}</td>'
   f'<td>{_safe(r.due_at)}</td>'
   f'<td>{_safe(r.status)}</td>'
   f'<td>{_safe(getattr(r, "response_text", None))}{" | " + _safe(getattr(r, "responded_by_email", None)) if getattr(r, "responded_by_email", None) else ""}</td>'
   f'</tr>'
   for r in _context_requests
 )
 + '</table>'
 if _context_requests else '<p class="meta">No context requests recorded for this case.</p>'}
{_render_context_request_provenance(_context_requests)}

<div class="section-title">SECTION 5C - ESCALATION</div>
<table>
  <tr><th>Escalated To Role</th><td>{_safe(getattr(case, "escalated_to_role", None))}</td></tr>
  <tr><th>Escalated To Email</th><td>{_safe(getattr(case, "escalated_to_email", None))}</td></tr>
  <tr><th>Escalated At</th><td>{_safe(getattr(case, "escalated_at", None))}</td></tr>
  <tr><th>Escalation Reason</th><td>{_safe(getattr(case, "escalation_reason", None))}</td></tr>
  <tr><th>Escalation Due Date</th><td>{_safe(getattr(case, "escalation_due_at", None))}</td></tr>
</table>

<div class="section-title">SECTION 6 - TELEMETRY INTEGRITY & PROVENANCE</div>
<pre>
Source System:              OpenEMR (read-only connector)
Connector:                  openemr_real
Telemetry Status:           {telemetry_label or '-'}
Telemetry Description:      {telemetry_desc or '-'}
Evidence Manifest Hash:     {evidence_manifest_hash}
Report ID:                  TP-{report_id}
Generated At:               {now.strftime('%Y-%m-%dT%H:%M:%SZ')}
Exported By:                {reviewer} ({reviewer_role})
OpenEMR Writeback:          DISABLED - TrustPulse operates in read-only mode
</pre>
{('<table><tr><th>Telemetry Status</th><th>Description</th><th>Rows Seen</th><th>Rows Inserted</th><th>Duplicates</th><th>Excluded Admin/System</th><th>Unsupported Rows</th><th>Parser Errors</th><th>Coverage Warning</th></tr>' + ''.join(
    f'<tr><td>{_safe(getattr(m, "telemetry_status_label", None))}</td>'
    f'<td>{_safe(getattr(m, "telemetry_status_description", None))}</td>'
    f'<td>{_safe(getattr(m, "source_rows_seen", None) or m.source_row_count)}</td>'
    f'<td>{_safe(getattr(m, "rows_inserted", None) or m.inserted_count)}</td>'
    f'<td>{_safe(m.duplicate_count)}</td>'
    f'<td>{_safe(getattr(m, "rows_excluded_system_admin", None))}</td>'
    f'<td>{_safe(getattr(m, "rows_unsupported_event_type", None))}</td>'
    f'<td>{_safe(m.parse_error_count)}</td>'
    f'<td>{_safe(getattr(m, "coverage_warning_reason", None))}</td></tr>'
    for m in _manifests
) + '</table>') if _manifests else ''}
<p><b>Source Ingestion Manifest(s) for case events:</b></p>
<table>
  <tr><th>#</th><th>Manifest ID</th><th>Started At</th><th>Events Ingested</th>
      <th>Source Batch SHA-256</th><th>Normalized Batch SHA-256</th><th>Manifest Hash (SHA-256)</th></tr>
  {''.join(
      f'<tr><td>{i+1}</td>'
      f'<td>{_safe(m.id)}</td>'
      f'<td class="meta">{_safe(m.started_at)}</td>'
      f'<td>{_safe(m.inserted_count)}</td>'
      f'<td class="meta">{_safe(m.source_batch_sha256 or "N/A")}</td>'
      f'<td class="meta">{_safe(getattr(m, "normalized_batch_sha256", None) or "N/A")}</td>'
      f'<td class="meta">{_safe(m.manifest_hash or "N/A")}</td>'
      f'</tr>'
      for i, m in enumerate(_manifests)
  ) if _manifests else "<tr><td colspan=7>No manifest data - events pre-date manifest tracking.</td></tr>"}
</table>

<div class="section-title">SECTION 7 - LIMITATIONS AND DISCLAIMER</div>
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


def _render_context_request_provenance(context_requests: list) -> str:
    provenance_rows = []
    for r in context_requests:
        prov = getattr(r, "recommendation_provenance", None)
        if not prov:
            continue
        template_id = _safe(prov.get("suggested_template_id", "-"))
        rationale = " · ".join(prov.get("suggested_rationale_labels") or []) or "-"
        generated = _safe(prov.get("generated_question_text", "-"))
        submitted = _safe(prov.get("final_submitted_question_text", r.question))
        edited = generated != submitted
        provenance_rows.append(
            f'<div class="ne-block" style="margin:4px 0">'
            f'<span class="ne-id">Context Request #{_safe(r.id)} — Recommendation Provenance</span><br>'
            f'<span class="meta">Template: {template_id}</span><br>'
            f'<span class="meta">Suggested because: {_safe(rationale)}</span><br>'
            f'<span class="meta">Generated question: {generated}</span><br>'
            f'<span class="meta">Submitted question: {submitted}</span><br>'
            f'<span class="meta">Reviewer edited before submission: {"YES" if edited else "NO"}</span>'
            f'</div>'
        )
    if not provenance_rows:
        return ""
    return (
        "<p><b>Context Request Recommendation Provenance</b></p>"
        + "".join(provenance_rows)
        + '<p class="meta">Generated question text is the system suggestion. '
        "Submitted question text is what the reviewer sent. "
        "TrustPulse does not present generated text as the official request unless the reviewer submitted it.</p>"
    )


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
<div style="margin:20px 0 8px;font-weight:bold;color:#0A2540;">Breach Risk Assessment (45 CFR §164.402)</div>
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


def _render_related_auth_signals(assessment: dict) -> str:
    signals = assessment.get("related_authentication_signals") or []
    if not signals:
        return ""
    rows = "".join(
        f'<tr><td>{_safe(s.get("event_type"))}</td>'
        f'<td>{_safe(s.get("count"))}</td>'
        f'<td class="meta">{_safe(s.get("time_window_start"))}</td>'
        f'<td class="meta">{_safe(s.get("time_window_end"))}</td>'
        f'<td>{_safe(s.get("explanation"))}</td></tr>'
        for s in signals
    )
    return (
        '<p><b>Related Authentication Signals</b></p>'
        '<table><tr><th>Event Type</th><th>Count</th><th>Window Start</th><th>Window End</th><th>Explanation</th></tr>'
        + rows + '</table>'
        '<p class="meta">Authentication events in ±1-hour window of the case time range for the same user. '
        'These are linked for context only and are not direct indicators of inappropriate access.</p>'
    )


def _render_review_section(case, assessment: dict) -> str:
    case_type = getattr(case, "case_type", "") or assessment.get("case_type", "")
    if case_type in ("AUTHENTICATION_REVIEW", "ACCOUNT_ACCESS_REVIEW", "ACCOUNT_MISUSE_REVIEW"):
        return _render_auth_review_section(assessment)
    return _render_patient_access_review_section(assessment)


def _render_auth_review_section(assessment: dict) -> str:
    checklist_items = assessment.get("quick_review_checks") or assessment.get("checklist_items") or []
    actions = assessment.get("suggested_review_actions") or []
    summary = assessment.get("authentication_summary") or {}
    primary_action = assessment.get("primary_suggested_action") or {}
    event_breakdown = assessment.get("event_type_breakdown") or {}
    return f"""
<div class="section-title">SECTION 3A - AUTHENTICATION REVIEW SUMMARY</div>
<div class="disclaimer">
  <b>Authentication Review Notice:</b> This section supports human review of login and account activity.
  It is not proof of account compromise, credential sharing, or inappropriate patient access.
</div>
<table>
  <tr><th>Case Type</th><td>{_safe(assessment.get("case_type"))}</td></tr>
  <tr><th>Review Summary</th><td>{_safe(assessment.get("summary"))}</td></tr>
  <tr><th>Subject User</th><td>{_safe(assessment.get("subject_user"))} ({_safe(assessment.get("subject_username"))})</td></tr>
  <tr><th>Subject Role</th><td>{_safe(assessment.get("subject_role"))}</td></tr>
  <tr><th>Linked Evidence Events</th><td>{_safe(assessment.get("linked_event_count"))}</td></tr>
  <tr><th>Failed Login Count</th><td>{_safe(summary.get("failed_login_count"))}</td></tr>
  <tr><th>Successful Login After Failures</th><td>{_safe("YES" if summary.get("successful_login_after_failures") else "NO")}</td></tr>
  <tr><th>Patient Access After Login</th><td>{_safe("YES" if summary.get("patient_access_after_login") else "NO")}</td></tr>
  <tr><th>Review Focus</th><td>{_safe(summary.get("review_focus"))}</td></tr>
  <tr><th>Cluster / Correlation Window</th><td>{_safe(summary.get("window_minutes"))} minutes</td></tr>
  <tr><th>Linked Sequence</th><td>{_safe(_auth_linked_sequence(assessment.get("linked_event_sequence") or []))}</td></tr>
</table>
<p><b>Quick Review Guide / Review Support</b></p>
<ul>{''.join(f'<li>{_safe(item)}</li>' for item in checklist_items)}</ul>
<p><b>Suggested Next Action</b></p>
<ul>
  {('<li><b>Primary:</b> ' + _safe(_display_action_label(primary_action)) + (' - ' + _safe(primary_action.get("reason_label")) if primary_action.get("reason_label") else '') + (' (' + _safe(primary_action.get("reason_code")) + ')' if primary_action.get("reason_code") else '') + '</li>') if primary_action else ''}
  {''.join(
      f'<li>{_safe(_display_action_label(item))}'
      f'{" - " + _safe(item.get("reason_label")) if item.get("reason_label") else ""}'
      f'{" (" + _safe(item.get("reason_code")) + ")" if item.get("reason_code") else ""}</li>'
      for item in actions[1:]
  )}
</ul>
<p><b>Authentication Summary</b></p>
<ul>
  <li>Event breakdown: {_safe(", ".join(f"{k}: {v}" for k, v in event_breakdown.items()) or "-")}</li>
  <li>Window minutes: {_safe(summary.get("window_minutes"))}</li>
</ul>
{_render_related_auth_signals(assessment)}
<p class="meta">This evidence package supports human compliance/privacy review and does not, by itself, establish account compromise or misuse.</p>
"""


def _render_patient_access_review_section(assessment: dict) -> str:
    missing_context = assessment.get("missing_context") or []
    actions = assessment.get("suggested_review_actions") or []
    checklist_items = assessment.get("quick_review_checks") or assessment.get("case_review_checklist") or assessment.get("checklist_items") or assessment.get("recommended_review_questions") or []
    suggested_reason_codes = assessment.get("suggested_reason_codes") or []
    primary_action = assessment.get("primary_suggested_action") or {}
    alternate_action = assessment.get("alternate_suggested_action") or {}
    missing_information_summary = assessment.get("missing_information_summary") or []
    technical_details = assessment.get("technical_details") or {}
    return f"""
<div class="section-title">SECTION 3A - PATIENT ACCESS APPROPRIATENESS REVIEW</div>
<div class="disclaimer">
  <b>Patient Access Review Notice:</b> This section supports human review of patient-access appropriateness.
  It is not an automatic finding of inappropriate access, breach, or HIPAA violation.
</div>
<table>
  <tr><th>Case Type</th><td>{_safe(assessment.get("case_type"))}</td></tr>
  <tr><th>Review Summary</th><td>{_safe(assessment.get("summary"))}</td></tr>
  <tr><th>Subject User</th><td>{_safe(assessment.get("subject_user"))} ({_safe(assessment.get("subject_username"))})</td></tr>
  <tr><th>Subject Role</th><td>{_safe(assessment.get("subject_role"))}</td></tr>
  <tr><th>Department</th><td>{_safe(assessment.get("subject_department"))}</td></tr>
  <tr><th>Linked Evidence Events</th><td>{_safe(assessment.get("linked_event_count"))}</td></tr>
  <tr><th>Unique Patient Tokens</th><td>{_safe(assessment.get("unique_patient_token_count"))}</td></tr>
  <tr><th>Quick Review Reason</th><td>{_safe(assessment.get("quick_review_reason") or assessment.get("summary"))}</td></tr>
  <tr><th>Guide Rationale</th><td>{_safe(assessment.get("guidance_rationale") or assessment.get("checklist_rationale"))}</td></tr>
</table>
<p><b>Quick Review Guide / Review Support</b></p>
<ul>
  {''.join(f'<li>{_safe(item)}</li>' for item in checklist_items)}
</ul>
<p><b>Suggested Next Action</b></p>
<ul>
  {('<li><b>Primary:</b> ' + _safe(_display_action_label(primary_action)) + (' - ' + _safe(primary_action.get("reason_label")) if primary_action.get("reason_label") else '') + (' (' + _safe(primary_action.get("reason_code")) + ')' if primary_action.get("reason_code") else '') + '</li>') if primary_action else ''}
  {('<li><b>Alternate:</b> ' + _safe(_display_action_label(alternate_action)) + (' - ' + _safe(alternate_action.get("reason_label")) if alternate_action.get("reason_label") else '') + (' (' + _safe(alternate_action.get("reason_code")) + ')' if alternate_action.get("reason_code") else '') + '</li>') if alternate_action else ''}
</ul>
<p><b>What Information Is Missing?</b></p>
<ul>
  {''.join(f'<li>{_safe(item)}</li>' for item in missing_information_summary) or '<li>No major information gaps were recorded.</li>'}
</ul>
<p class="meta">This evidence package supports human compliance/privacy review. It is not an automatic finding of inappropriate access, breach, or HIPAA violation.</p>
<p><b>Detailed Missing Context</b></p>
{''.join(
    f'<div class="ne-block"><span class="ne-id">{_safe(item.get("context"))}</span><br>'
    f'<span class="meta">Status: {_safe(item.get("status"))}</span><br>'
    f'<span class="meta">Reason: {_safe(item.get("reason"))}</span><br>'
    f'<span class="meta">Effect on confidence: {_safe(item.get("effect_on_confidence"))}</span></div>'
    for item in missing_context
) or '<p class="meta">No explicit missing-context limitations were recorded.</p>'}
<p><b>Suggested Review Actions</b></p>
<ul>
  {''.join(
      f'<li>{_safe(item.get("action"))}'
      f'{" - " + _safe(item.get("reason_label")) if item.get("reason_label") else ""}'
      f'{" (" + _safe(item.get("reason_code")) + ")" if item.get("reason_code") else ""}'
      f': {_safe(item.get("rationale"))}</li>'
      for item in actions
  )}
</ul>
<p><b>Suggested Reason Codes</b></p>
<ul>
  {''.join(
      f'<li>{_safe(item.get("action"))} → {_safe(item.get("reason_code"))}'
      f'{" - " + _safe(item.get("reason_label")) if item.get("reason_label") else ""}</li>'
      for item in suggested_reason_codes
  ) or '<li>No structured reason-code suggestions available.</li>'}
</ul>
<p><b>Technical Details</b></p>
<ul>
  <li>Triggered rules: {_safe(", ".join(f"{item.get('rule_id')} {item.get('rule_name')}" for item in (technical_details.get("triggered_rules") or [])) or "-")}</li>
  <li>Baseline maturity: {_safe(technical_details.get("baseline_maturity") or assessment.get("baseline_maturity"))}</li>
  <li>Baseline note: {_safe(technical_details.get("baseline_note") or "-")}</li>
  <li>Missing source details: {_safe(" | ".join(technical_details.get("missing_source_details") or []) or "-")}</li>
  <li>History days available: {_safe(technical_details.get("history_days_available") or "-")}</li>
  <li>History baseline maturity: {_safe(technical_details.get("history_maturity_label") or "-")}</li>
  <li>Baseline basis: {_safe(technical_details.get("baseline_basis") or "-")}</li>
</ul>
{_render_related_auth_signals(assessment)}
"""
