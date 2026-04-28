"""
TrustPulse Periodic Governance Report Generator.

Produces self-contained HTML designed for small-clinic compliance officers.
Print to PDF via the in-page button (browser print dialog → Save as PDF).
No external dependencies - pure HTML + inline CSS.
"""
import hashlib
import os
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
    NormalizedEvent, Case, ComplianceCalendarItem,
    IngestionManifest,
)
from engine.compliance import (
    compute_compliance_health_score,
    compute_minimum_necessary,
    seed_compliance_calendar,
)

CLINIC_NAME = os.environ.get("CLINIC_NAME", "Healthcare Clinic")

_SEV = {
    "P0_CRITICAL": ("#FEE2E2", "#991B1B", "Critical"),
    "P1_HIGH":     ("#FFEDD5", "#9A3412", "High"),
    "P2_MEDIUM":   ("#FEF9C3", "#854D0E", "Medium"),
    "P3_LOW":      ("#DCFCE7", "#166534", "Low"),
}

_RULE_NAMES = {
    "R-01": "After-Hours Access",
    "R-02": "Bulk Patient Access",
    "R-03": "Weekend Access",
    "R-04": "Cross-Department Access",
    "R-05": "VIP / No-Appointment Access",
    "R-06": "Failed Login Burst",
    "R-07": "Modify-Then-Export",
    "R-08": "Access Volume Spike",
    "R-09": "New IP Address",
    "R-10": "Admin Action After Hours",
}


def _s(v) -> str:
    return escape(str(v)) if v is not None else "-"


def _bar(pct: float, color: str = "#6366F1", height: int = 10) -> str:
    pct = min(max(float(pct), 0), 100)
    return (
        f'<div style="background:#E2E8F0;border-radius:4px;height:{height}px;'
        f'width:100%;overflow:hidden;">'
        f'<div style="background:{color};height:100%;width:{pct:.1f}%;border-radius:4px;"></div>'
        f'</div>'
    )


def _risk_color(score: float) -> str:
    if score >= 80: return "#DC2626"
    if score >= 60: return "#EA580C"
    if score >= 30: return "#D97706"
    return "#16A34A"


def _pill(text: str, bg: str, fg: str) -> str:
    return (f'<span style="background:{bg};color:{fg};padding:2px 9px;'
            f'border-radius:4px;font-size:11px;font-weight:700;">{_s(text)}</span>')


def _status_pill(status: str) -> str:
    m = {
        "OPEN":          ("#FEE2E2", "#991B1B"),
        "ESCALATED":     ("#FFEDD5", "#9A3412"),
        "REVIEWED":      ("#DCFCE7", "#166534"),
        "DISMISSED":     ("#F1F5F9", "#475569"),
        "SUPPRESSED":    ("#F1F5F9", "#475569"),
        "FALSE_POSITIVE": ("#EEF2FF", "#3730A3"),
    }
    bg, fg = m.get(status, ("#F1F5F9", "#475569"))
    return _pill(status, bg, fg)


def _cal_pill(status: str) -> str:
    m = {
        "ON_TRACK": ("#DCFCE7", "#166534"),
        "DUE_SOON": ("#FEF9C3", "#854D0E"),
        "OVERDUE":  ("#FEE2E2", "#991B1B"),
    }
    bg, fg = m.get(status, ("#F1F5F9", "#475569"))
    return _pill(status, bg, fg)


def _build_recommendations(open_cases, critical_cases, breach_cases,
                            health, calendar_items, min_nec) -> list:
    recs = []
    if critical_cases:
        recs.append(
            f"<b>{len(critical_cases)} critical case(s) are open</b> and require immediate review. "
            f"Critical cases should be reviewed within 24 hours per your incident response policy."
        )
    if breach_cases:
        recs.append(
            f"<b>{len(breach_cases)} case(s) carry a potential breach-risk flag.</b> "
            f"Conduct a formal breach risk assessment under 45 CFR §164.402 "
            f"to determine whether OCR notification may be required within 60 days of discovery."
        )
    overdue_cal = [i for i in calendar_items if i.status == "OVERDUE"]
    if overdue_cal:
        names = ", ".join(i.item_type.replace("_", " ").title() for i in overdue_cal)
        recs.append(
            f"<b>HIPAA calendar items are overdue:</b> {names}. "
            f"Schedule these immediately to avoid documentation gaps during an audit."
        )
    action_users = [u for u in min_nec if u["status"] == "ACTION"]
    if action_users:
        recs.append(
            f"<b>{len(action_users)} staff member(s) show access volume 3× or more above peers.</b> "
            f"Review whether this access is clinically justified under the minimum necessary "
            f"standard (§164.502(b))."
        )
    if health["score"] < 70:
        recs.append(
            f"<b>Overall compliance health is {health['score']}/100 (Grade {health['grade']}).</b> "
            f"Primary issue: {health['top_issue']}. Focus on closing open cases to improve this score."
        )
    stale = [c for c in open_cases
             if c.created_at and (datetime.utcnow() - c.created_at).days > 7]
    if stale:
        recs.append(
            f"<b>{len(stale)} case(s) have been open for more than 7 days</b> without disposition. "
            f"Dispose of these to maintain a clean audit queue."
        )
    if not recs:
        recs.append(
            "No immediate actions required. Continue your regular audit review cadence and "
            "ensure all HIPAA calendar items remain on schedule. Well done."
        )
    return recs


def generate_periodic_report(
    db: Session,
    period_days: int = 7,
    period_label: str = "Weekly",
    clinic_name: Optional[str] = None,
    generated_by: str = "TrustPulse",
    report_id: Optional[str] = None,
) -> str:
    clinic = clinic_name or CLINIC_NAME
    now    = datetime.utcnow()
    cutoff = now - timedelta(days=period_days)

    if not report_id:
        slug       = hashlib.md5(now.isoformat().encode()).hexdigest()[:6].upper()
        report_id  = f"TP-{now.strftime('%Y%m%d')}-{slug}"

    # ── Queries ────────────────────────────────────────────────────────────────
    total_events  = (db.query(NormalizedEvent)
                     .filter(NormalizedEvent.event_time >= cutoff).count())

    all_cases     = (db.query(Case).filter(Case.created_at >= cutoff).all())
    open_cases    = [c for c in all_cases if c.status == "OPEN"]
    critical_cases = [c for c in open_cases if c.severity == "P0_CRITICAL"]
    breach_cases  = [c for c in all_cases if c.breach_risk]

    health = compute_compliance_health_score(db)
    seed_compliance_calendar(db)
    calendar_items = (db.query(ComplianceCalendarItem)
                      .order_by(ComplianceCalendarItem.next_due).all())
    min_nec = compute_minimum_necessary(db)

    hourly_raw = (
        db.query(NormalizedEvent.hour_of_day, func.count(NormalizedEvent.id))
        .filter(NormalizedEvent.event_time >= cutoff)
        .group_by(NormalizedEvent.hour_of_day).all()
    )
    hourly     = {h: c for h, c in hourly_raw}
    max_hourly = max(hourly.values(), default=1)

    rules_raw = (
        db.query(NormalizedEvent.triggered_rules)
        .filter(NormalizedEvent.event_time >= cutoff,
                NormalizedEvent.triggered_rules.isnot(None)).all()
    )
    rule_counts: dict = defaultdict(int)
    for (rules,) in rules_raw:
        if rules:
            for r in rules:
                if r.get("fired") and not r.get("not_evaluated"):
                    rule_counts[r["rule_id"]] += 1

    user_stats = (
        db.query(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            NormalizedEvent.user_role,
            func.count(NormalizedEvent.id).label("event_count"),
            func.max(NormalizedEvent.risk_score).label("max_risk"),
        )
        .filter(NormalizedEvent.event_time >= cutoff)
        .group_by(NormalizedEvent.user_id, NormalizedEvent.user_name, NormalizedEvent.user_role)
        .order_by(func.max(NormalizedEvent.risk_score).desc())
        .limit(10).all()
    )

    last_manifest = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS")
        .order_by(IngestionManifest.completed_at.desc()).first()
    )

    recommendations = _build_recommendations(
        open_cases, critical_cases, breach_cases, health, calendar_items, min_nec
    )

    # ── Assemble sections ──────────────────────────────────────────────────────
    health_score = health["score"]
    health_color = "#16A34A" if health_score >= 80 else "#D97706" if health_score >= 60 else "#DC2626"
    comp          = health["components"]

    # Breach banner
    breach_banner = ""
    if breach_cases:
        breach_banner = f"""
        <div style="background:#FEF2F2;border:2px solid #FCA5A5;border-radius:10px;
                    padding:16px 20px;margin-bottom:24px;display:flex;gap:14px;align-items:flex-start;">
          <div style="font-size:24px;line-height:1;">&#9888;</div>
          <div>
            <div style="font-weight:700;color:#991B1B;font-size:14px;margin-bottom:4px;">
              Potential Breach Risk Detected - {len(breach_cases)} Case(s)
            </div>
            <div style="font-size:13px;color:#7F1D1D;line-height:1.6;">
              One or more cases have been flagged with potential breach risk. A formal breach
              risk assessment under 45 CFR §164.402 may be required. OCR notification must be
              completed within 60 days of discovery. Use TrustPulse to initiate a breach
              assessment from the Cases page.
            </div>
          </div>
        </div>"""

    # Cases table
    sorted_cases = sorted(all_cases, key=lambda c: c.risk_score or 0, reverse=True)[:20]
    case_rows = ""
    for c in sorted_cases:
        bg, fg, lbl = _SEV.get(c.severity, ("#F1F5F9", "#475569", c.severity))
        rc = _risk_color(c.risk_score or 0)
        case_rows += f"""
        <tr>
          <td>{_pill(lbl, bg, fg)}</td>
          <td style="font-weight:600;">{_s(c.user_name or c.user_id)}</td>
          <td>{_s((c.pattern_type or "").replace("_", " ").title())}</td>
          <td style="width:160px;">
            <div style="display:flex;align-items:center;gap:8px;">
              {_bar(c.risk_score or 0, rc, 8)}
              <span style="font-size:12px;font-weight:700;color:{rc};min-width:28px;">{int(c.risk_score or 0)}</span>
            </div>
          </td>
          <td>{_status_pill(c.status)}</td>
          <td style="font-size:11px;color:#64748B;">
            {c.date_end.strftime("%b %d, %Y") if c.date_end else "-"}
          </td>
        </tr>"""
    if not case_rows:
        case_rows = '<tr><td colspan="6" style="text-align:center;color:#94A3B8;padding:24px;">No cases in this period</td></tr>'

    # Hourly bars
    hourly_html = ""
    for h in range(24):
        count = hourly.get(h, 0)
        pct   = (count / max_hourly * 100) if max_hourly > 0 else 0
        color = "#6366F1" if 7 <= h < 19 else "#F59E0B"
        hourly_html += f"""
        <div style="display:flex;flex-direction:column;align-items:center;flex:1;gap:2px;">
          <div style="font-size:8px;color:#94A3B8;font-weight:600;height:12px;line-height:12px;">
            {count if count else ""}
          </div>
          <div style="width:100%;height:56px;background:#F1F5F9;border-radius:3px 3px 0 0;
                      display:flex;align-items:flex-end;overflow:hidden;">
            <div style="width:100%;height:{pct:.0f}%;background:{color};border-radius:3px 3px 0 0;"></div>
          </div>
          <div style="font-size:8px;color:#94A3B8;">{h:02d}</div>
        </div>"""

    # Rule frequency table
    rule_rows = ""
    if rule_counts:
        max_rc = max(rule_counts.values(), default=1)
        for rid, count in sorted(rule_counts.items(), key=lambda x: -x[1]):
            pct = count / max_rc * 100
            rule_rows += f"""
            <tr>
              <td style="font-family:monospace;font-size:12px;color:#6366F1;font-weight:700;">{_s(rid)}</td>
              <td style="font-weight:500;">{_s(_RULE_NAMES.get(rid, rid))}</td>
              <td style="width:180px;">{_bar(pct, "#6366F1", 8)}</td>
              <td style="text-align:right;font-weight:700;">{count:,}</td>
            </tr>"""
    if not rule_rows:
        rule_rows = '<tr><td colspan="4" style="text-align:center;color:#94A3B8;padding:20px;">No rules fired this period</td></tr>'

    # User stats table
    user_rows = ""
    for row in user_stats:
        rc = _risk_color(row.max_risk or 0)
        user_rows += f"""
        <tr>
          <td style="font-weight:600;">{_s(row.user_name or row.user_id)}</td>
          <td>{_pill(row.user_role or "staff", "#EEF2FF", "#4338CA")}</td>
          <td style="text-align:right;font-weight:600;">{row.event_count:,}</td>
          <td style="width:160px;">
            <div style="display:flex;align-items:center;gap:8px;">
              {_bar(row.max_risk or 0, rc, 8)}
              <span style="font-size:12px;font-weight:700;color:{rc};min-width:28px;">{int(row.max_risk or 0)}</span>
            </div>
          </td>
        </tr>"""
    if not user_rows:
        user_rows = '<tr><td colspan="4" style="text-align:center;color:#94A3B8;padding:20px;">No activity in this period</td></tr>'

    # Calendar table
    cal_rows = ""
    for item in calendar_items:
        last = item.last_completed.strftime("%b %d, %Y") if item.last_completed else '<span style="color:#DC2626;font-weight:600;">Never recorded</span>'
        nxt  = item.next_due.strftime("%b %d, %Y") if item.next_due else "-"
        cal_rows += f"""
        <tr>
          <td style="font-weight:600;">{_s(item.item_type.replace("_", " ").title())}</td>
          <td style="font-family:monospace;font-size:11px;color:#6366F1;">{_s(item.hipaa_provision)}</td>
          <td>{last}</td>
          <td>{nxt}</td>
          <td>{_cal_pill(item.status)}</td>
        </tr>"""

    # Minimum necessary table
    mn_rows = ""
    for u in min_nec[:15]:
        row_bg = "#FEF2F2" if u["status"] == "ACTION" else "#FFFBEB" if u["status"] == "REVIEW" else "#fff"
        mn_rows += f"""
        <tr style="background:{row_bg};">
          <td style="font-weight:600;">{_s(u["user_name"])}</td>
          <td>{_pill(u["role"], "#EEF2FF", "#4338CA")}</td>
          <td style="text-align:right;font-weight:600;">{u["user_daily_avg"]}</td>
          <td style="text-align:right;color:#64748B;">{u["peer_daily_avg"]}</td>
          <td style="text-align:right;font-weight:700;">{u["ratio"]}×</td>
          <td>{_status_pill(u["status"]) if u["status"] in ("ACTION","REVIEW") else _pill("NORMAL","#DCFCE7","#166534")}</td>
        </tr>"""
    if not mn_rows:
        mn_rows = '<tr><td colspan="6" style="text-align:center;color:#94A3B8;padding:20px;">No data available</td></tr>'

    # Recommendations
    rec_html = ""
    for i, rec in enumerate(recommendations):
        icon = "&#9888;" if i == 0 and len(recommendations) > 1 else "&#8594;"
        rec_html += f"""
        <div style="display:flex;gap:12px;margin-bottom:12px;padding:14px 16px;
                    background:#F8FAFC;border-left:4px solid #6366F1;border-radius:0 8px 8px 0;">
          <div style="font-size:16px;flex-shrink:0;color:#6366F1;">{icon}</div>
          <div style="font-size:13px;color:#1E293B;line-height:1.7;">{rec}</div>
        </div>"""

    # Health component bars
    comp_bars = ""
    for label, val in [
        ("Audit Review Rate",       comp["audit_review_rate"]),
        ("Mean Time to Review",     comp["mean_time_to_review_score"]),
        ("No Open Critical Cases",  comp["open_p0_cases_score"]),
        ("Suppression Quality",     comp["suppression_quality"]),
        ("Log Completeness",        comp["log_completeness"]),
    ]:
        c = "#16A34A" if val >= 70 else "#D97706" if val >= 40 else "#DC2626"
        comp_bars += f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
          <div style="min-width:210px;font-size:12px;color:#475569;font-weight:500;">{label}</div>
          <div style="flex:1;">{_bar(val, c, 10)}</div>
          <div style="min-width:40px;text-align:right;font-size:12px;font-weight:700;color:{c};">{val:.0f}%</div>
        </div>"""

    manifest_hash  = last_manifest.manifest_hash if last_manifest else "N/A"
    source_hash    = last_manifest.source_batch_sha256 if last_manifest else "N/A"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TrustPulse - {_s(period_label)} Report - {_s(clinic)}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
        background:#F8FAFC;color:#1E293B;font-size:14px;line-height:1.5}}
  .page{{max-width:960px;margin:0 auto;padding:32px 24px}}
  .rpt-header{{background:#0A2540;color:#fff;border-radius:12px;padding:28px 32px;
               margin-bottom:28px;position:relative}}
  .rpt-header h1{{font-size:22px;font-weight:800;letter-spacing:-.01em;margin-bottom:4px}}
  .print-btn{{position:absolute;top:24px;right:24px;background:rgba(255,255,255,.15);
              border:1px solid rgba(255,255,255,.3);color:#fff;padding:8px 18px;
              border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}}
  .print-btn:hover{{background:rgba(255,255,255,.25)}}
  .section{{background:#fff;border:1px solid #E2E8F0;border-radius:12px;
            margin-bottom:24px;overflow:hidden}}
  .sec-hdr{{background:#F8FAFC;border-bottom:1px solid #E2E8F0;padding:13px 24px;
            font-size:11px;font-weight:800;text-transform:uppercase;
            letter-spacing:.08em;color:#475569}}
  .sec-body{{padding:24px}}
  .kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}
  .kpi{{background:#fff;border:1px solid #E2E8F0;border-radius:10px;padding:20px}}
  .kpi-lbl{{font-size:11px;font-weight:700;color:#64748B;text-transform:uppercase;
            letter-spacing:.06em;margin-bottom:10px}}
  .kpi-val{{font-size:32px;font-weight:800;color:#0F172A;line-height:1}}
  .kpi-sub{{font-size:12px;color:#94A3B8;margin-top:6px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;padding:10px 14px;font-size:11px;font-weight:700;
      text-transform:uppercase;letter-spacing:.06em;color:#64748B;
      border-bottom:2px solid #F1F5F9;background:#FAFAFA;white-space:nowrap}}
  td{{padding:11px 14px;border-bottom:1px solid #F8FAFC;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#FAFAFA}}
  .disclaimer{{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:10px;
               padding:16px 20px;margin-bottom:24px;font-size:12px;color:#1E3A5F;line-height:1.7}}
  .footer{{margin-top:32px;padding:20px 24px;background:#F8FAFC;
           border:1px solid #E2E8F0;border-radius:10px;font-size:11px;color:#94A3B8}}
  @media print{{
    body{{background:#fff}}
    .print-btn{{display:none!important}}
    .page{{max-width:100%;padding:0}}
    .rpt-header{{border-radius:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
    .section{{border-radius:0;page-break-inside:avoid}}
    .kpi-grid{{grid-template-columns:repeat(4,1fr)}}
    @page{{margin:1.5cm;size:A4}}
  }}
</style>
</head>
<body>
<div class="page">

<div class="rpt-header">
  <button class="print-btn" onclick="window.print()">&#9113; Print / Save as PDF</button>
  <div style="font-size:11px;color:#64748B;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px;">
    TrustPulse Governance Report
  </div>
  <h1>{_s(period_label)} Audit Governance Report</h1>
  <div style="font-size:14px;color:#94A3B8;margin-top:4px;">{_s(clinic)}</div>
  <div style="font-size:12px;color:#64748B;margin-top:10px;">
    Report ID: {_s(report_id)} &nbsp;&middot;&nbsp;
    Period: {cutoff.strftime("%b %d, %Y")} &ndash; {now.strftime("%b %d, %Y")} ({period_days} days)
    &nbsp;&middot;&nbsp; Generated: {now.strftime("%B %d, %Y at %H:%M UTC")}
    &nbsp;&middot;&nbsp; By: {_s(generated_by)}
  </div>
</div>

<div class="disclaimer">
  <b>Governance Decision-Support Report.</b>&nbsp; This document is based on TrustPulse-observed
  OpenEMR telemetry only. It is not a legal determination of HIPAA compliance, breach status, or
  notification obligation. Final determinations require qualified privacy and compliance review
  under 45 CFR §164.402&ndash;414. Retain this report for 6 years per §164.530(j).
</div>

{breach_banner}

<!-- 1. Executive Summary -->
<div class="section">
  <div class="sec-hdr">1 &mdash; Executive Summary</div>
  <div class="sec-body">
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-lbl">Events Ingested</div>
        <div class="kpi-val">{total_events:,}</div>
        <div class="kpi-sub">Last {period_days} days</div>
      </div>
      <div class="kpi">
        <div class="kpi-lbl">Open Cases</div>
        <div class="kpi-val" style="color:{'#DC2626' if open_cases else '#16A34A'};">{len(open_cases)}</div>
        <div class="kpi-sub">Requiring review</div>
      </div>
      <div class="kpi">
        <div class="kpi-lbl">Critical Alerts</div>
        <div class="kpi-val" style="color:{'#DC2626' if critical_cases else '#16A34A'};">{len(critical_cases)}</div>
        <div class="kpi-sub">P0 severity</div>
      </div>
      <div class="kpi">
        <div class="kpi-lbl">Compliance Score</div>
        <div class="kpi-val" style="color:{health_color};">{health_score}</div>
        <div class="kpi-sub">Grade: {health['grade']}</div>
      </div>
    </div>
  </div>
</div>

<!-- 2. Compliance Health -->
<div class="section">
  <div class="sec-hdr">2 &mdash; Compliance Health Score &mdash; HIPAA §164.308(a)(1)</div>
  <div class="sec-body">
    <div style="display:flex;gap:40px;align-items:flex-start;">
      <div style="min-width:130px;text-align:center;padding-top:8px;">
        <div style="font-size:56px;font-weight:800;color:{health_color};line-height:1;">{health_score}</div>
        <div style="font-size:13px;color:#64748B;margin-top:2px;">out of 100</div>
        <div style="font-size:18px;font-weight:700;color:{health_color};margin-top:4px;">Grade: {health['grade']}</div>
        <div style="margin-top:12px;font-size:12px;color:#64748B;line-height:1.5;">{_s(health['top_issue'])}</div>
      </div>
      <div style="flex:1;">
        <div style="font-size:12px;color:#64748B;margin-bottom:16px;font-style:italic;">
          Composite of: audit review rate (30%), mean time to review (20%),
          open critical cases (25%), suppression quality (15%), log completeness (10%).
        </div>
        {comp_bars}
      </div>
    </div>
  </div>
</div>

<!-- 3. Cases -->
<div class="section">
  <div class="sec-hdr">3 &mdash; Cases Requiring Review (Top 20 by Risk Score)</div>
  <div class="sec-body" style="padding:0;">
    <table>
      <thead>
        <tr><th>Severity</th><th>User</th><th>Pattern</th><th>Risk Score</th><th>Status</th><th>Last Activity</th></tr>
      </thead>
      <tbody>{case_rows}</tbody>
    </table>
  </div>
</div>

<!-- 4. Hourly Distribution -->
<div class="section">
  <div class="sec-hdr">4 &mdash; Access Activity &mdash; Hourly Distribution</div>
  <div class="sec-body">
    <div style="font-size:12px;color:#64748B;margin-bottom:16px;">
      <span style="display:inline-flex;align-items:center;gap:6px;margin-right:20px;">
        <span style="width:12px;height:12px;background:#6366F1;border-radius:2px;display:inline-block;"></span>
        Business hours (07:00&ndash;19:00)
      </span>
      <span style="display:inline-flex;align-items:center;gap:6px;">
        <span style="width:12px;height:12px;background:#F59E0B;border-radius:2px;display:inline-block;"></span>
        After-hours
      </span>
    </div>
    <div style="display:flex;align-items:flex-end;gap:2px;">
      {hourly_html}
    </div>
    <div style="font-size:11px;color:#94A3B8;margin-top:6px;text-align:center;">Hour of day (UTC, 00&ndash;23)</div>
  </div>
</div>

<!-- 5. Rules -->
<div class="section">
  <div class="sec-hdr">5 &mdash; Detection Rules &mdash; Fire Frequency This Period</div>
  <div class="sec-body" style="padding:0;">
    <table>
      <thead><tr><th>Rule</th><th>Name</th><th>Frequency</th><th style="text-align:right;">Count</th></tr></thead>
      <tbody>{rule_rows}</tbody>
    </table>
  </div>
</div>

<!-- 6. Top Users -->
<div class="section">
  <div class="sec-hdr">6 &mdash; Top Users by Peak Risk Score</div>
  <div class="sec-body" style="padding:0;">
    <table>
      <thead><tr><th>User</th><th>Role</th><th style="text-align:right;">Events</th><th>Peak Risk Score</th></tr></thead>
      <tbody>{user_rows}</tbody>
    </table>
  </div>
</div>

<!-- 7. Calendar -->
<div class="section">
  <div class="sec-hdr">7 &mdash; HIPAA Compliance Calendar</div>
  <div class="sec-body" style="padding:0;">
    <table>
      <thead><tr><th>Item</th><th>HIPAA Provision</th><th>Last Completed</th><th>Next Due</th><th>Status</th></tr></thead>
      <tbody>{cal_rows}</tbody>
    </table>
  </div>
</div>

<!-- 8. Minimum Necessary -->
<div class="section">
  <div class="sec-hdr">8 &mdash; Minimum Necessary Access &mdash; §164.502(b)</div>
  <div class="sec-body" style="padding:0;">
    <div style="padding:12px 24px 12px;font-size:12px;color:#64748B;border-bottom:1px solid #F1F5F9;">
      Users with access ≥3× their peer average are flagged <b>ACTION</b>; 2&ndash;3× are <b>REVIEW</b>.
      These flags support your minimum necessary compliance obligation.
    </div>
    <table>
      <thead><tr><th>User</th><th>Role</th><th style="text-align:right;">Avg/Day</th><th style="text-align:right;">Peer Avg</th><th style="text-align:right;">Ratio</th><th>Status</th></tr></thead>
      <tbody>{mn_rows}</tbody>
    </table>
  </div>
</div>

<!-- 9. Recommendations -->
<div class="section">
  <div class="sec-hdr">9 &mdash; Recommendations</div>
  <div class="sec-body">{rec_html}</div>
</div>

<!-- 10. Integrity -->
<div class="section">
  <div class="sec-hdr">10 &mdash; Report Integrity &amp; Provenance</div>
  <div class="sec-body" style="padding:0;">
    <table>
      <tr><td style="font-weight:600;width:220px;padding:12px 14px;">Report ID</td>
          <td style="font-family:monospace;font-size:12px;">{_s(report_id)}</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">Source System</td>
          <td>OpenEMR - read-only connector (openemr_real)</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">Last Ingestion Hash</td>
          <td style="font-family:monospace;font-size:11px;word-break:break-all;">{manifest_hash}</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">Source Batch SHA-256</td>
          <td style="font-family:monospace;font-size:11px;word-break:break-all;">{source_hash}</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">Generated At</td>
          <td>{now.strftime("%Y-%m-%dT%H:%M:%SZ")}</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">Generated By</td>
          <td>{_s(generated_by)}</td></tr>
      <tr><td style="font-weight:600;padding:12px 14px;">OpenEMR Writeback</td>
          <td style="color:#16A34A;font-weight:600;">DISABLED - Read-only mode enforced</td></tr>
    </table>
  </div>
</div>

<div class="footer">
  TrustPulse v0.3.0 - Healthcare Governance Platform for Small Clinics &nbsp;&middot;&nbsp;
  This report is a governance decision-support artifact. It does not constitute a final HIPAA
  compliance determination, breach notification, or legal finding.
  Retain for 6 years per §164.530(j).
</div>

</div>
</body>
</html>"""
