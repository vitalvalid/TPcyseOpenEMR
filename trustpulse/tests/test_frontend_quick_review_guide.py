from pathlib import Path


def test_quick_review_guide_modal():
    html = Path(__file__).resolve().parents[1].joinpath("frontend", "index.html").read_text()
    modal_start = html.index("/* ── QUICK REVIEW GUIDE MODAL ── */")
    modal_end = html.index("})()}", modal_start)
    modal = html[modal_start:modal_end]
    cases_start = html.index("function CasesPage({cases,user,onCaseClick})")
    cases_end = html.index("// ─── Employees Page", cases_start)
    cases_section = html[cases_start:cases_end]

    # ── Design system additions ──
    assert "drawer-handle" in html
    assert "filter-chip" in html
    assert "HEALTH_LABEL" in html
    assert "Telemetry Health" in html
    # Drawer resize: width stored in localStorage
    assert "tp_drawer_w" in html
    assert "clampDrawerW" in html
    # Dashboard: Needs Context and Escalated KPI cards
    assert "Needs Context" in html
    assert "Escalated" in html
    assert "tp_cases_status" in html
    # Cases: case type filter
    assert "caseTypeFilter" in cases_section
    assert "All Patterns" in cases_section
    # Cases: filter chips
    assert "filter-chip-x" in cases_section
    assert "Reset" in cases_section
    # Sidebar: grouped navigation
    assert "nav-sec-label" in html
    assert "{group:'Review'}" in html
    assert "{group:'Monitoring'}" in html
    # Premium CSS design system classes
    assert "tab-bar" in html
    assert "tab-btn" in html
    assert "sum-tile" in html
    assert "sum-tile-lbl" in html
    assert "sum-tile-val" in html
    assert "why-review" in html
    assert "action-panel" in html
    assert "tl-entry" in html
    assert "action-notice-ok" in html
    assert "action-notice-info" in html
    # Dynamic visual components
    assert "useCountUp" in html
    assert "RiskRing" in html
    assert "fmtRelTime" in html
    assert "CaseCard" in html
    assert "case-card" in html
    assert "ACTION_ICON" in html
    assert "kpi-pulse" in html
    assert "numVal" in html

    assert "Open Review Guide" in html
    assert "review-guide-modal" in modal
    assert "review-guide-hero" in modal
    assert "review-guide-stepper" not in modal
    assert "Quick Review Guide" in modal
    assert "Understand" not in modal
    assert "Verify" not in modal
    assert "Case summary" in modal
    assert "Verify before closing" not in modal
    assert "Things to confirm" in modal
    assert "Recommended action" in modal
    assert "Suggested message" in modal
    assert "Missing context" in modal
    assert "Use recommended action" in modal
    assert "Copy message" in modal
    assert "Close" in modal
    assert "Physician access review" in html
    assert "Billing access review" in html
    assert "Editable after applying in the review form." not in modal

    assert "Suggested because:" not in modal
    assert "Generated from:" not in modal
    assert "Technical details" not in modal
    assert "Show other options" not in modal
    assert "Triggered rules" not in modal
    assert "Suggested reason-code mappings" not in modal
    assert "NEED_APPOINTMENT_CONTEXT" not in modal
    assert "NEED_BILLING_JUSTIFICATION" not in modal
    assert "HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW" not in modal
    assert "R-02" not in modal
    assert "R-08" not in modal
    assert "COLD_START" not in modal
    assert "? button" not in modal
    assert "info button" not in modal
    assert "help button" not in modal
    assert "review-check-indicator" not in modal

    assert "setDisposition(action.action);" in html
    assert "setPendingSuggestedReason('');" in html
    assert "setReasonCode('');" in html
    assert "setContextQuestion(prev=>prev?.trim()?prev:action.question_text);" in html
    assert "actionFormRef.current?.scrollIntoView({behavior:'smooth',block:'start'});" in html
    assert "actionFormRef.current?.focus();" in html
    assert "Action prepared in the review form." in html
    assert "const [sortBy,setSortBy]=useState('latest_event_time');" in html
    assert "const ROUTE_TO_PAGE =" in html
    assert "function parseHashRoute(hash=window.location.hash)" in html
    assert "function buildHashRoute(page='dashboard',caseId=null)" in html
    assert "window.addEventListener('hashchange',syncRoute);" in html
    assert "if(!window.location.hash){" in html
    assert "window.location.replace(buildHashRoute('dashboard'));" in html
    assert "applyHashRoute('cases',caseId);" in html
    assert "applyHashRoute('cases');" in html
    assert "connection:'system'" in html
    assert "'permission-groups':'admin_permissions'" in html
    assert "All Triage" not in cases_section
    assert "<th>Triage</th>" not in cases_section
    assert "<th>Breach</th>" not in cases_section
    assert "AFTER_HOURS_LOGIN_ATTEMPT:'After-hours login'" in html
    assert "SUCCESSFUL_LOGIN_AFTER_FAILURES:'Login after failures'" in html
    assert "FAILED_LOGIN_BURST:'Failed login burst'" in html
    assert "CASE_TYPE_LABEL" in html
    assert "!isAuthCase(c)" in html
    assert "unique_patient_token_count||0" in html
    assert "if(action.action==='REQUEST_CONTEXT') return action.action_label||'Request account confirmation';" in html
    assert "(paa?.modifiers||c?.modifiers||[]).forEach" in html
    assert "Suggested next step: {suggestedNextStepLabel(c,c.patient_access_assessment)}" in html
    assert "onClick={()=>onCaseClick(c.case_id)}" in html
    assert "setLoadError('This case was refreshed. Please reload the queue.');" in html
    assert "onClose={closeCaseRoute}" in html

    assert "Context Requests" in html
    assert "Escalation Ownership" in html
    assert "requested_from_role" in html
    assert "escalated_to_role" in html

    # ── Review Configuration page ──
    assert "ReviewConfigPage" in html
    assert "baselineMaturityInfo" in html
    assert "review_config" in html
    assert "config:'review_config'" in html
    assert "Cold Start" in html
    assert "Preliminary" in html
    assert "Partial" in html
    assert "Stable" in html
    assert "Failed Login Threshold" in html
    assert "Failed Login Burst" in html
    assert "Monitored Entities" in html
    assert "Baseline Readiness" in html
    assert "machine learning" not in html.lower()
    assert "machine-learning" not in html.lower()
