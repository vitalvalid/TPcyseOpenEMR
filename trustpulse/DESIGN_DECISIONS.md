# TrustPulse - Design Decisions

## 1. Why read-only DB access, not FHIR?

Audit logs are not FHIR resources. The FHIR standard covers clinical data sharing
(patients, observations, medications) - not access-event governance. TrustPulse
reads directly from the OpenEMR `log` table and, where available, joins `api_log`
for request/IP metadata. The connector executes SELECT-only queries through a
dedicated read-only database account (`trustpulse_ro`). An SQL allowlist function
validates every query before execution. This gives direct, structured access to
every access event without requiring OpenEMR plugin installation or FHIR
configuration changes.

## 2. Why SQLite for the governance store?

Zero configuration, single-file, and fully portable - ideal for a PoC that ships
via `docker compose up`. The governance store is append-heavy (events are written
once, never updated except for status fields) and single-writer (one FastAPI
process), which are exactly the use cases where SQLite excels. The production
upgrade path is PostgreSQL with append-only partitioning and a tamper-evidence
trigger log.

## 3. Why rule-based scoring instead of the Bayesian Network from Deliverable 2?

**Explainability is non-negotiable for compliance.** When a compliance officer
must explain a flagged event to an HHS Office for Civil Rights (OCR) investigator,
they need to cite a specific, articulable reason - not a model confidence score.
Each TrustPulse rule maps directly to a HIPAA CFR provision.

The Bayesian Network from Deliverable 2 is used as a risk-reasoning and
control-prioritization artifact. The current PoC implements Phase 1 deterministic
rule-based scoring because this is more transparent and easier to validate for
compliance review. Runtime Bayesian calibration is reserved for future work after
sufficient labeled disposition history is available.

## 4. Why not block access in real-time?

TrustPulse is a **governance oversight tool**, not an access control system.
Real-time blocking would require modifying OpenEMR authentication code (prohibited
by course constraints) or sitting inline as a network proxy (introduces
availability risk to a clinical system). Post-hoc detection preserves clinical
workflow availability while surfacing violations within minutes for human review.

## 5. Authentication and RBAC

All TrustPulse API endpoints require a JWT bearer token. Four roles are supported:

| Role | Permissions |
|------|-------------|
| `TRUSTPULSE_ADMIN` | All permissions (default bootstrap role) |
| `COMPLIANCE_OFFICER` | review, disposition, export, breach_assessment |
| `AUDITOR` | review, export |
| `SECURITY_ADMIN` | configure, trigger_ingestion, review |

Reviewer identity is derived from the authenticated JWT - disposition requests
cannot supply an arbitrary reviewer name.

## 6. The false assurance risk

A green TrustPulse dashboard does not mean the system is clean. If OpenEMR's
audit logging is misconfigured, a network partition occurs, or the connector
raises a `RuntimeError` (unreachable DB), TrustPulse records the failure in the
manifest with status `FAILED` and the ingestion status API returns
`SOURCE_UNREACHABLE`. An empty result set and a connection failure are
explicitly distinguished.

Gap detection compares the last ingested source ID to the first ID in the new
batch, catching gaps between batches (not only within them).

## 7. Patient privacy in API responses

Patient IDs are never returned raw through the API. The case detail endpoint
returns a `patient_token` (HMAC-SHA256 with a deployment-specific secret) instead
of `patient_id`. Evidence reports use the same tokenization.

## Known Limitations (PoC Scope)

- The PoC depends on OpenEMR audit-log coverage. Some actions may not be logged
  by OpenEMR or may not expose patient/context metadata.
- Context-dependent rules (R-05 appointment, R-04 department, R-09 IP history)
  return `not_evaluated` when the required data is unavailable.
- The optional demo scenario generator is lab-only; do not run against production.
- SQLite is used for PoC portability; production would use PostgreSQL.
- JWT revocation is not implemented.
- Evidence packages are HTML; cryptographic signing (PDF/PAdES) is a Phase 2 item.
- The Bayesian Network from Deliverable 2 informs risk reasoning but is not
  implemented as a runtime scoring component.
- No email/SIEM alerting (stub exists, not wired to a real transport).
- No multi-tenant support.
