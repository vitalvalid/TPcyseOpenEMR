from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RuleResult:
    rule_id:             str
    rule_name:           str
    fired:               bool
    score_contribution:  float
    description:         str
    hipaa_ref:           str = ""
    severity:            str = "MEDIUM"
    confidence:          str = "HIGH"       # HIGH / MEDIUM / LOW
    supporting_fields:   list = field(default_factory=list)
    limitations:         list = field(default_factory=list)
    not_evaluated:       bool = False
    not_evaluated_reason: str = ""


BUSINESS_HOURS_START = 7
BUSINESS_HOURS_END   = 19


def r01_after_hours(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    hour  = event.get("hour_of_day", 12)
    fired = hour < BUSINESS_HOURS_START or hour >= BUSINESS_HOURS_END
    return RuleResult(
        rule_id="R-01", rule_name="After-Hours Access",
        fired=fired, score_contribution=20.0 if fired else 0.0,
        description=f"Access at {hour:02d}:00 - outside business hours (07:00–19:00)",
        hipaa_ref="HIPAA 45 CFR §164.312(a)(2)(i)",
        severity="MEDIUM",
        confidence="HIGH",
        supporting_fields=["event_time", "hour_of_day"],
    )


def r02_bulk_patient_access(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    import os
    daily    = context.get("daily_unique_patients", 0)
    maturity = (baseline or {}).get("maturity", "COLD_START")
    avg      = (baseline or {}).get("avg_unique_patients", 0)
    cold     = maturity in ("COLD_START", "TRAINING") or not avg
    cold_thresh = int(os.environ.get("TRUSTPULSE_BULK_ACCESS_THRESHOLD", "10"))
    thresh   = cold_thresh if cold else max(avg * 2.5, 15)
    fired    = daily >= thresh and daily > 0
    lims     = ([f"Cold-start threshold {thresh} applied (configurable via "
                 "TRUSTPULSE_BULK_ACCESS_THRESHOLD); will rise once baseline matures."]
                if cold else [])
    return RuleResult(
        rule_id="R-02", rule_name="Bulk Patient Access",
        fired=fired, score_contribution=25.0 if fired else 0.0,
        description=(
            f"Accessed {daily} unique patients today "
            f"(threshold {thresh:.0f}, baseline avg {avg:.1f}, maturity={maturity})"
        ),
        hipaa_ref="HIPAA 45 CFR §164.308(a)(1)",
        severity="HIGH",
        confidence="HIGH" if (baseline and not cold) else "LOW",
        supporting_fields=["daily_unique_patients"],
        limitations=lims,
    )


def r03_weekend_access(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    dow  = event.get("day_of_week", 0)
    dept = (event.get("department") or "").lower()
    fired = dow >= 5 and "emergency" not in dept and " er" not in dept
    return RuleResult(
        rule_id="R-03", rule_name="Weekend Access",
        fired=fired, score_contribution=15.0 if fired else 0.0,
        description="Weekend access by non-emergency department user",
        hipaa_ref="HIPAA 45 CFR §164.312(b)",
        severity="MEDIUM",
        confidence="HIGH",
        supporting_fields=["day_of_week", "department"],
    )


def r04_cross_department(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    user_dept    = (context.get("user_department") or "").strip().lower()
    patient_dept = (event.get("department") or "").strip().lower()

    if not user_dept or not patient_dept:
        return RuleResult(
            rule_id="R-04", rule_name="Cross-Department Access",
            fired=False, score_contribution=0.0,
            description="Cross-department access",
            hipaa_ref="HIPAA 45 CFR §164.312(a)(1)",
            not_evaluated=True,
            not_evaluated_reason=(
                "Department context unavailable - "
                "users.facility or specialty not accessible in OpenEMR."
            ),
            limitations=["Requires reliable department data from OpenEMR users table."],
        )

    fired = user_dept != patient_dept and event.get("event_type") == "patient_access"
    return RuleResult(
        rule_id="R-04", rule_name="Cross-Department Access",
        fired=fired, score_contribution=20.0 if fired else 0.0,
        description=(
            f"Cross-department access: user in '{user_dept}' "
            f"accessed patient from '{patient_dept}'"
        ),
        hipaa_ref="HIPAA 45 CFR §164.312(a)(1)",
        severity="HIGH",
        confidence="MEDIUM",
        supporting_fields=["department", "user_department"],
        limitations=["Department mapping depends on OpenEMR users.facility field accuracy."],
    )


def r05_vip_patient(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    has_appt = context.get("has_appointment")   # None = not available

    if has_appt is None:
        return RuleResult(
            rule_id="R-05", rule_name="VIP/No-Appointment Access",
            fired=False, score_contribution=0.0,
            description="VIP patient accessed without a corresponding appointment",
            hipaa_ref="HIPAA 45 CFR §164.312(b)",
            not_evaluated=True,
            not_evaluated_reason=(
                "Appointment context unavailable - "
                "openemr_postcalendar_events table not accessible."
            ),
            limitations=["Requires openemr_postcalendar_events in OpenEMR database."],
        )

    is_vip = context.get("patient_is_vip", False)
    fired  = is_vip and not has_appt
    return RuleResult(
        rule_id="R-05", rule_name="VIP/No-Appointment Access",
        fired=fired, score_contribution=30.0 if fired else 0.0,
        description="VIP patient accessed without a corresponding appointment on record",
        hipaa_ref="HIPAA 45 CFR §164.312(b)",
        severity="HIGH",
        confidence="MEDIUM",
        supporting_fields=["patient_id", "patient_is_vip", "has_appointment"],
    )


def r06_failed_logins(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    failures = context.get("recent_failed_logins", 0)

    if failures == 0 and event.get("event_type") != "failed_login":
        return RuleResult(
            rule_id="R-06", rule_name="Failed-Login Burst",
            fired=False, score_contribution=0.0,
            description="Failed-login burst (3+ failures in 10 minutes)",
            hipaa_ref="HIPAA 45 CFR §164.312(d)",
            not_evaluated=False,
            limitations=[
                "Fires only when OpenEMR logs failed-login events in the log table. "
                "If OpenEMR does not log failed logins accessibly, this rule will "
                "never fire - this is a limitation, not a finding."
            ],
        )

    fired = failures >= 3
    return RuleResult(
        rule_id="R-06", rule_name="Failed-Login Burst",
        fired=fired, score_contribution=35.0 if fired else 0.0,
        description=f"{failures} failed login attempts within the last 10 minutes",
        hipaa_ref="HIPAA 45 CFR §164.312(d)",
        severity="CRITICAL",
        confidence="HIGH",
        supporting_fields=["recent_failed_logins"],
    )


def r07_modify_then_export(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    fired = context.get("modify_then_export_within_5min", False)

    if event.get("event_type") not in ("report_export", "record_modify"):
        return RuleResult(
            rule_id="R-07", rule_name="Modify-Then-Export",
            fired=False, score_contribution=0.0,
            description="Record modification followed by export within 5 minutes",
            hipaa_ref="HIPAA 45 CFR §164.312(c)(1)",
            not_evaluated=False,
            limitations=[
                "Requires both record_modify and report_export event types to be "
                "present in OpenEMR logs."
            ],
        )

    return RuleResult(
        rule_id="R-07", rule_name="Modify-Then-Export",
        fired=fired, score_contribution=25.0 if fired else 0.0,
        description="Record modification followed by export within 5 minutes",
        hipaa_ref="HIPAA 45 CFR §164.312(c)(1)",
        severity="HIGH",
        confidence="HIGH",
        supporting_fields=["event_type", "modify_then_export_within_5min"],
    )


def r08_volume_spike(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    import os
    daily    = context.get("daily_access_count", 0)
    maturity = (baseline or {}).get("maturity", "COLD_START")
    avg      = (baseline or {}).get("avg_daily_accesses", 0)
    std      = (baseline or {}).get("std_daily_accesses", 5)
    cold     = maturity in ("COLD_START", "TRAINING") or not avg
    cold_thresh = int(os.environ.get("TRUSTPULSE_VOLUME_SPIKE_THRESHOLD", "20"))
    thresh   = cold_thresh if cold else avg + 3 * max(std, 1)
    fired    = daily >= thresh
    lims     = ([f"Cold-start threshold {thresh} applied (configurable via "
                 "TRUSTPULSE_VOLUME_SPIKE_THRESHOLD); 3σ threshold activates once baseline matures."]
                if cold else [])
    return RuleResult(
        rule_id="R-08", rule_name="Access Volume Spike",
        fired=fired, score_contribution=20.0 if fired else 0.0,
        description=(
            f"Daily access count {daily} exceeds threshold "
            f"{thresh:.0f} (avg={avg:.1f}, σ={std:.1f}, maturity={maturity})"
        ),
        hipaa_ref="HIPAA 45 CFR §164.308(a)(1)",
        severity="HIGH",
        confidence="HIGH" if (baseline and not cold) else "LOW",
        supporting_fields=["daily_access_count"],
        limitations=lims,
    )


def r09_new_ip(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    ip        = event.get("ip_address", "")
    known_ips = (baseline or {}).get("known_ips", [])

    if not ip:
        return RuleResult(
            rule_id="R-09", rule_name="New IP Address",
            fired=False, score_contribution=0.0,
            description="Access from previously unseen IP address",
            hipaa_ref="HIPAA 45 CFR §164.312(b)",
            not_evaluated=True,
            not_evaluated_reason=(
                "IP address not present in this event - "
                "api_log may not be available or event predates API logging."
            ),
            limitations=["Requires ip_address from api_log table."],
        )

    if not known_ips:
        return RuleResult(
            rule_id="R-09", rule_name="New IP Address",
            fired=False, score_contribution=0.0,
            description=f"Access from IP {ip} - no baseline IPs yet",
            hipaa_ref="HIPAA 45 CFR §164.312(b)",
            not_evaluated=False,
            limitations=["Insufficient baseline history to compare IPs."],
        )

    fired = ip not in known_ips
    return RuleResult(
        rule_id="R-09", rule_name="New IP Address",
        fired=fired, score_contribution=10.0 if fired else 0.0,
        description=f"Access from previously unseen IP: {ip}",
        hipaa_ref="HIPAA 45 CFR §164.312(b)",
        severity="MEDIUM",
        confidence="MEDIUM",
        supporting_fields=["ip_address", "known_ips"],
    )


def r10_admin_after_hours(event: dict, baseline: Optional[dict], context: dict) -> RuleResult:
    hour        = event.get("hour_of_day", 12)
    is_admin    = event.get("event_type") == "admin_action"
    after_hours = hour < BUSINESS_HOURS_START or hour >= BUSINESS_HOURS_END
    fired       = is_admin and after_hours
    return RuleResult(
        rule_id="R-10", rule_name="Admin Action After Hours",
        fired=fired, score_contribution=30.0 if fired else 0.0,
        description=f"Administrative action outside business hours at {hour:02d}:00",
        hipaa_ref="HIPAA 45 CFR §164.308(a)(3)",
        severity="HIGH",
        confidence="HIGH",
        supporting_fields=["event_type", "hour_of_day"],
    )


ALL_RULES = [
    r01_after_hours,
    r02_bulk_patient_access,
    r03_weekend_access,
    r04_cross_department,
    r05_vip_patient,
    r06_failed_logins,
    r07_modify_then_export,
    r08_volume_spike,
    r09_new_ip,
    r10_admin_after_hours,
]


def evaluate_all_rules(
    event: dict, baseline: Optional[dict], context: dict
) -> List[RuleResult]:
    return [rule(event, baseline, context) for rule in ALL_RULES]
