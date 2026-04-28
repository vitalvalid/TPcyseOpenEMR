"""
Reports API - scheduled and ad-hoc governance report generation.

Endpoints:
  GET  /api/reports/schedules          - list schedules
  POST /api/reports/schedules          - create a schedule
  PUT  /api/reports/schedules/{id}     - update a schedule
  DELETE /api/reports/schedules/{id}   - delete a schedule
  POST /api/reports/run                - generate a report now (ad-hoc or from schedule)
  GET  /api/reports/history            - list past runs (metadata only)
  GET  /api/reports/history/{id}       - download the HTML of a past run
"""
import os
import smtplib
from datetime import datetime, timedelta
from email import encoders as _enc
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import ScheduledReport, ReportRun
from db.session import get_tp_session
from governance.report_generator import generate_periodic_report
from api.auth import require_permission, TrustPulseUser

router = APIRouter(prefix="/api/reports", tags=["reports"])

CLINIC_NAME = os.environ.get("CLINIC_NAME", "Healthcare Clinic")

_FREQUENCY_DAYS = {
    "WEEKLY":    7,
    "MONTHLY":   30,
    "QUARTERLY": 90,
}


# ── Schedules ──────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    name:               str
    frequency:          str                   # WEEKLY / MONTHLY / QUARTERLY / CUSTOM
    frequency_days:     int       = 7         # only used when frequency == CUSTOM
    recipient_email:    Optional[str] = None  # placeholder - email sending not yet wired
    include_period_days: int      = 7         # how much history the report covers


class ScheduleUpdate(BaseModel):
    name:               Optional[str] = None
    recipient_email:    Optional[str] = None
    is_active:          Optional[bool] = None
    include_period_days: Optional[int] = None


def _next_run(frequency: str, frequency_days: int) -> datetime:
    days = _FREQUENCY_DAYS.get(frequency, frequency_days)
    return datetime.utcnow() + timedelta(days=days)


@router.get("/schedules")
def list_schedules(
    db: Session = Depends(get_tp_session),
    _u: TrustPulseUser = Depends(require_permission("review")),
):
    schedules = db.query(ScheduledReport).order_by(ScheduledReport.created_at.desc()).all()
    return {"schedules": [_schedule_dict(s) for s in schedules]}


@router.post("/schedules", status_code=201)
def create_schedule(
    req: ScheduleCreate,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("export")),
):
    freq_days = _FREQUENCY_DAYS.get(req.frequency, req.frequency_days)
    s = ScheduledReport(
        name               = req.name,
        frequency          = req.frequency,
        frequency_days     = freq_days,
        recipient_email    = req.recipient_email,
        is_active          = True,
        created_by         = current.email,
        created_at         = datetime.utcnow(),
        next_run_at        = _next_run(req.frequency, freq_days),
        include_period_days = req.include_period_days,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _schedule_dict(s)


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    req: ScheduleUpdate,
    db: Session = Depends(get_tp_session),
    _u: TrustPulseUser = Depends(require_permission("export")),
):
    s = db.get(ScheduledReport, schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if req.name is not None:
        s.name = req.name
    if req.recipient_email is not None:
        s.recipient_email = req.recipient_email
    if req.is_active is not None:
        s.is_active = req.is_active
    if req.include_period_days is not None:
        s.include_period_days = req.include_period_days
    db.commit()
    db.refresh(s)
    return _schedule_dict(s)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(get_tp_session),
    _u: TrustPulseUser = Depends(require_permission("export")),
):
    s = db.get(ScheduledReport, schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(s)
    db.commit()


# ── Report generation ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    schedule_id:  Optional[int] = None
    period_days:  int = 7
    report_type:  str = "ADHOC"   # WEEKLY / MONTHLY / QUARTERLY / ADHOC


@router.post("/run")
def run_report(
    req: RunRequest,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("export")),
):
    """Generate a report immediately. Returns the ReportRun metadata (not the HTML)."""
    schedule: Optional[ScheduledReport] = None
    period_days = req.period_days
    report_type = req.report_type

    if req.schedule_id:
        schedule = db.get(ScheduledReport, req.schedule_id)
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")
        period_days = schedule.include_period_days
        report_type = schedule.frequency

    period_label = _period_label(report_type, period_days)

    run = ReportRun(
        schedule_id   = req.schedule_id,
        schedule_name = schedule.name if schedule else "Ad-hoc Report",
        report_type   = report_type,
        period_days   = period_days,
        generated_at  = datetime.utcnow(),
        generated_by  = current.email,
        status        = "IN_PROGRESS",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        html = generate_periodic_report(
            db,
            period_days   = period_days,
            period_label  = period_label,
            clinic_name   = CLINIC_NAME,
            generated_by  = current.email,
        )
        run.html_content    = html
        run.file_size_bytes = len(html.encode())
        run.status          = "SUCCESS"

        # Update schedule tracking
        if schedule:
            schedule.last_run_at = datetime.utcnow()
            schedule.next_run_at = _next_run(schedule.frequency, schedule.frequency_days)

        # Email placeholder - log intent, do not send
        if schedule and schedule.recipient_email:
            run.email_sent_to = schedule.recipient_email
            run.email_status  = "SKIPPED"   # replace with SMTP/SendGrid call here

        db.commit()
    except Exception as exc:
        run.status        = "FAILED"
        run.error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    return {
        "run_id":           run.id,
        "status":           run.status,
        "report_type":      run.report_type,
        "period_days":      run.period_days,
        "generated_at":     run.generated_at.isoformat(),
        "file_size_bytes":  run.file_size_bytes,
        "download_url":     f"/api/reports/history/{run.id}",
        "email_sent_to":    run.email_sent_to,
        "email_status":     run.email_status,
    }


# ── History ────────────────────────────────────────────────────────────────────

@router.get("/history")
def list_history(
    limit: int = Query(default=20, le=100),
    db: Session = Depends(get_tp_session),
    _u: TrustPulseUser = Depends(require_permission("review")),
):
    runs = (
        db.query(ReportRun)
        .order_by(ReportRun.generated_at.desc())
        .limit(limit)
        .all()
    )
    return {"runs": [_run_dict(r) for r in runs]}


@router.get("/history/{run_id}", response_class=HTMLResponse)
def get_report_html(
    run_id: int,
    db: Session = Depends(get_tp_session),
    _u: TrustPulseUser = Depends(require_permission("review")),
):
    run = db.get(ReportRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Report run not found")
    if run.status != "SUCCESS" or not run.html_content:
        raise HTTPException(status_code=404, detail="Report HTML not available")
    filename = f"trustpulse_report_{run.generated_at.strftime('%Y%m%d')}_{run.id}.html"
    return HTMLResponse(
        content=run.html_content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Email send ────────────────────────────────────────────────────────────────

class SendRequest(BaseModel):
    to: str


@router.post("/history/{run_id}/send")
def send_report_email(
    run_id: int,
    req: SendRequest,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("export")),
):
    """Send a saved report as an HTML email attachment."""
    run = db.get(ReportRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Report not found")
    if run.status != "SUCCESS" or not run.html_content:
        raise HTTPException(status_code=404, detail="Report HTML not available")

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    if not smtp_host:
        raise HTTPException(
            status_code=503,
            detail="SMTP not configured - set SMTP_HOST, SMTP_USER, SMTP_PASS in .env",
        )

    filename = f"trustpulse_report_{run.generated_at.strftime('%Y%m%d')}_{run.id}.html"

    msg = MIMEMultipart()
    msg["Subject"] = (
        f"TrustPulse Governance Report - {run.report_type} "
        f"({run.generated_at.strftime('%Y-%m-%d')})"
    )
    msg["From"] = from_addr
    msg["To"] = req.to
    msg.attach(MIMEText(
        f"Please find the attached {run.report_type} governance report "
        f"generated on {run.generated_at.strftime('%Y-%m-%d %H:%M')} UTC "
        f"by {run.generated_by}.",
        "plain",
    ))

    attachment = MIMEBase("text", "html")
    attachment.set_payload(run.html_content.encode())
    _enc.encode_base64(attachment)
    attachment.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(attachment)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if smtp_user:
                smtp.login(smtp_user, smtp_pass)
            smtp.sendmail(from_addr, [req.to], msg.as_string())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Email delivery failed: {exc}")

    run.email_sent_to = req.to
    run.email_status  = "SENT"
    db.commit()
    return {"status": "sent", "to": req.to}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _period_label(report_type: str, period_days: int) -> str:
    return {
        "WEEKLY":    "Weekly",
        "MONTHLY":   "Monthly",
        "QUARTERLY": "Quarterly",
    }.get(report_type, f"{period_days}-Day")


def _schedule_dict(s: ScheduledReport) -> dict:
    return {
        "id":                  s.id,
        "name":                s.name,
        "frequency":           s.frequency,
        "frequency_days":      s.frequency_days,
        "include_period_days": s.include_period_days,
        "recipient_email":     s.recipient_email,
        "is_active":           s.is_active,
        "created_by":          s.created_by,
        "created_at":          s.created_at.isoformat() if s.created_at else None,
        "last_run_at":         s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at":         s.next_run_at.isoformat() if s.next_run_at else None,
    }


def _run_dict(r: ReportRun) -> dict:
    return {
        "id":              r.id,
        "schedule_id":     r.schedule_id,
        "schedule_name":   r.schedule_name,
        "report_type":     r.report_type,
        "period_days":     r.period_days,
        "generated_at":    r.generated_at.isoformat() if r.generated_at else None,
        "generated_by":    r.generated_by,
        "status":          r.status,
        "file_size_bytes": r.file_size_bytes,
        "error_message":   r.error_message,
        "email_sent_to":   r.email_sent_to,
        "email_status":    r.email_status,
        "download_url":    f"/api/reports/history/{r.id}" if r.status == "SUCCESS" else None,
    }


# ── Background runner (called from main.py poller) ─────────────────────────────

def run_due_scheduled_reports(db: Session) -> int:
    """Called from the background poller. Returns count of reports run."""
    now = datetime.utcnow()
    due = (
        db.query(ScheduledReport)
        .filter(
            ScheduledReport.is_active == True,
            ScheduledReport.next_run_at <= now,
        )
        .all()
    )
    count = 0
    for schedule in due:
        run = ReportRun(
            schedule_id   = schedule.id,
            schedule_name = schedule.name,
            report_type   = schedule.frequency,
            period_days   = schedule.include_period_days,
            generated_at  = now,
            generated_by  = "scheduled",
            status        = "IN_PROGRESS",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        try:
            html = generate_periodic_report(
                db,
                period_days  = schedule.include_period_days,
                period_label = _period_label(schedule.frequency, schedule.include_period_days),
                clinic_name  = CLINIC_NAME,
                generated_by = f"Scheduled - {schedule.name}",
            )
            run.html_content    = html
            run.file_size_bytes = len(html.encode())
            run.status          = "SUCCESS"
            if schedule.recipient_email:
                run.email_sent_to = schedule.recipient_email
                run.email_status  = "SKIPPED"   # wire real email here
        except Exception as exc:
            run.status        = "FAILED"
            run.error_message = str(exc)
        schedule.last_run_at = now
        schedule.next_run_at = _next_run(schedule.frequency, schedule.frequency_days)
        db.commit()
        count += 1
    return count
