#!/usr/bin/env python3
"""
OpenEMR seed script.
Creates: 1 renamed facility, 5 staff users (2 physicians, 1 nurse, 1 billing, 1 admin),
and 50 synthetic patients.
Runs once inside the seed container after OpenEMR is healthy.
Idempotent -- safe to run multiple times.
"""
import os
import sys
import time
import random
from datetime import datetime, timedelta, time as dtime

import pymysql
import bcrypt
from faker import Faker

DB_HOST      = os.getenv("MYSQL_HOST",          "mariadb")
DB_USER      = os.getenv("MYSQL_USER",          "openemr")
DB_PASS      = os.getenv("MYSQL_PASSWORD",      "openemrpass")
DB_NAME      = os.getenv("MYSQL_DATABASE",      "openemr")

FAKE = Faker()
Faker.seed(42)
random.seed(42)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def wait_for_db(timeout=300):
    print("Waiting for OpenEMR DB to be ready...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                                   database=DB_NAME, connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'patient_data'")
                if cur.fetchone():
                    conn.close()
                    print("OpenEMR DB is ready.", flush=True)
                    return
            conn.close()
        except Exception:
            pass
        print("  Not ready yet, retrying in 5s...", flush=True)
        time.sleep(5)
    print("ERROR: OpenEMR DB never became ready.", flush=True)
    sys.exit(1)


def connect():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


# ---------------------------------------------------------------------------
# Facility
# ---------------------------------------------------------------------------

def seed_facility(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM facility ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row and row["name"] != "Demo Health Clinic":
            cur.execute("UPDATE facility SET name='Demo Health Clinic' WHERE id=%s", (row["id"],))
            conn.commit()
            print(f"Renamed facility {row['id']} to 'Demo Health Clinic'.", flush=True)
        else:
            print("Facility already set, skipping.", flush=True)


def get_facility_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM facility ORDER BY id LIMIT 1")
        row = cur.fetchone()
        return row["id"] if row else 1


# ---------------------------------------------------------------------------
# ACL group helpers
# ---------------------------------------------------------------------------

def get_group_id(conn, value: str, fallback_value: str = "doc") -> int:
    """Return gacl_aro_groups.id for the given value, falling back to fallback_value."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM gacl_aro_groups WHERE value=%s LIMIT 1", (value,))
        row = cur.fetchone()
        if row:
            return row["id"]
        # fallback
        cur.execute("SELECT id FROM gacl_aro_groups WHERE value=%s LIMIT 1", (fallback_value,))
        row = cur.fetchone()
        return row["id"] if row else 13


def next_gacl_aro_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(id) AS m FROM gacl_aro")
        row = cur.fetchone()
        return (row["m"] or 0) + 1


# ---------------------------------------------------------------------------
# Staff users
#
# specialty values must match TrustPulse ROLE_MAP in openemr_real.py:
#   "Internal Medicine" / "Family Medicine" / "Oncology" → clinician
#   "Nursing"           → nurse
#   "Billing"           → billing
#   "Administration"    → admin
#
# acl_group: OpenEMR gacl_aro_groups.value — falls back to 'doc' if missing.
# title / taxonomy: NPI taxonomy code for each role.
# ---------------------------------------------------------------------------

STAFF = [
    # Physicians
    {
        "username":  "dr_nguyen",
        "fname":     "Michael",
        "lname":     "Nguyen",
        "specialty": "Internal Medicine",
        "title":     "Dr.",
        "taxonomy":  "207R00000X",   # Internal Medicine
        "acl_group": "doc",
        "password":  "Doctor@2026",
    },
    {
        "username":  "dr_patel",
        "fname":     "Priya",
        "lname":     "Patel",
        "specialty": "Family Medicine",
        "title":     "Dr.",
        "taxonomy":  "207Q00000X",   # Family Medicine
        "acl_group": "doc",
        "password":  "Doctor@2026",
    },
    # Nursing
    {
        "username":  "nurse_chen",
        "fname":     "Linda",
        "lname":     "Chen",
        "specialty": "Nursing",
        "title":     "RN",
        "taxonomy":  "163W00000X",   # Registered Nurse
        "acl_group": "nursing",      # falls back to 'doc' if not present
        "password":  "Doctor@2026",
    },
    # Billing
    {
        "username":  "billing_ross",
        "fname":     "David",
        "lname":     "Ross",
        "specialty": "Billing",
        "title":     "",
        "taxonomy":  "",
        "acl_group": "bill",         # falls back to 'doc' if not present
        "password":  "Doctor@2026",
    },
    # Administration
    {
        "username":  "admin_hayes",
        "fname":     "Susan",
        "lname":     "Hayes",
        "specialty": "Administration",
        "title":     "",
        "taxonomy":  "",
        "acl_group": "admin",        # falls back to 'doc' if not present
        "password":  "Doctor@2026",
    },
]


def seed_staff(conn, facility_id):
    for person in STAFF:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username=%s", (person["username"],))
            if cur.fetchone():
                print(f"User {person['username']} already exists, skipping.", flush=True)
                continue

            authorized = 1 if person["acl_group"] in ("doc", "nursing") else 0
            cur.execute(
                """
                INSERT INTO users
                    (username, password, fname, lname, facility, facility_id,
                     active, authorized, see_auth, email_direct,
                     upin, npi, title, specialty, taxonomy)
                VALUES (%s, '', %s, %s, 'Demo Health Clinic', %s,
                        1, %s, 1, '',
                        %s, %s, %s, %s, %s)
                """,
                (
                    person["username"],
                    person["fname"],
                    person["lname"],
                    facility_id,
                    authorized,
                    f"D{random.randint(10000, 99999)}",
                    f"{random.randint(1000000000, 9999999999)}",
                    person["title"],
                    person["specialty"],
                    person["taxonomy"],
                ),
            )
            user_id = cur.lastrowid

            pw_hash = bcrypt.hashpw(
                person["password"].encode(), bcrypt.gensalt(rounds=10)
            ).decode()
            # OpenEMR validates bcrypt hashes via PHP, which emits/accepts the
            # "$2y$" prefix. Python bcrypt emits "$2b$", so normalize it.
            pw_hash = pw_hash.replace("$2b$", "$2y$", 1)
            cur.execute(
                """
                INSERT INTO users_secure (id, username, password, last_update_password, last_update)
                VALUES (%s, %s, %s, NOW(), NOW())
                """,
                (user_id, person["username"], pw_hash),
            )

            # OpenEMR login expects a row in the legacy groups table in addition
            # to phpGACL mappings. Without this, authentication fails with
            # "user not found in a group" even though ACL rows exist.
            cur.execute(
                """
                INSERT INTO groups (name, user)
                VALUES ('Default', %s)
                """,
                (person["username"],),
            )

            group_id = get_group_id(conn, person["acl_group"])
            aro_id   = next_gacl_aro_id(conn)
            cur.execute(
                """
                INSERT INTO gacl_aro (id, section_value, value, order_value, name, hidden)
                VALUES (%s, 'users', %s, 10, %s, 0)
                """,
                (aro_id, person["username"], f"{person['fname']} {person['lname']}"),
            )
            cur.execute(
                "INSERT INTO gacl_groups_aro_map (group_id, aro_id) VALUES (%s, %s)",
                (group_id, aro_id),
            )

            conn.commit()
            print(
                f"Created {person['specialty']} user {person['username']}"
                f" (user_id={user_id}, acl_group={person['acl_group']}, group_id={group_id}).",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

def seed_patients(conn, count=50):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM patient_data")
        if cur.fetchone()["n"] >= count:
            print("Patients already seeded, skipping.", flush=True)
            return

        cur.execute("SELECT id FROM users WHERE username='dr_nguyen' LIMIT 1")
        row = cur.fetchone()
        provider_id = row["id"] if row else 1

        cur.execute("SELECT id FROM sequences LIMIT 1")
        row = cur.fetchone()
        next_pid = row["id"] if row else 1

        inserted = 0
        for i in range(count):
            pid  = next_pid + i
            sex  = random.choice(["Male", "Female"])
            fname = FAKE.first_name_male() if sex == "Male" else FAKE.first_name_female()
            lname = FAKE.last_name()
            dob   = FAKE.date_of_birth(minimum_age=18, maximum_age=85)
            ssn   = (
                f"{random.randint(100,899):03d}-"
                f"{random.randint(10,99):02d}-"
                f"{random.randint(1000,9999):04d}"
            )

            cur.execute(
                """
                INSERT INTO patient_data
                    (pid, pubpid, title, fname, lname, sex, DOB, date,
                     street, city, state, postal_code, country_code,
                     phone_home, phone_cell, email, ss,
                     status, providerID, language)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, NOW(),
                     %s, %s, %s, %s, 'US',
                     %s, %s, %s, %s,
                     'active', %s, 'English')
                """,
                (
                    pid, str(pid),
                    "Mr." if sex == "Male" else "Ms.",
                    fname, lname, sex, dob.isoformat(),
                    FAKE.street_address()[:255],
                    FAKE.city()[:30],
                    FAKE.state_abbr(),
                    FAKE.postcode()[:10],
                    FAKE.phone_number()[:20],
                    FAKE.phone_number()[:20],
                    FAKE.email(),
                    ssn,
                    provider_id,
                ),
            )
            inserted += 1

        cur.execute("UPDATE sequences SET id = id + %s", (inserted,))
        conn.commit()
        print(f"Seeded {inserted} patients (pids {next_pid}-{next_pid+inserted-1}).", flush=True)


# ---------------------------------------------------------------------------
# Demo audit logs
#
# Inserts realistic log entries so TrustPulse generates 15-20 cases on first
# startup without requiring manual scenario generation.
#
# Scenarios per period (4 periods × ~7 days apart = 4 different week buckets):
#   VOLUME_SPIKE  — 25 patient-access events in a single day (triggers R-02 + R-08)
#   OFF_HOURS     — patient-access or admin events at 3 am / 9 pm (R-01)
#   CREDENTIAL_RISK — 4 failed logins within 10 minutes (R-06)
# ---------------------------------------------------------------------------

def seed_audit_logs(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM log WHERE user='dr_nguyen' AND event='patient-record'"
        )
        if cur.fetchone()["n"] >= 20:
            print("Demo audit logs already seeded, skipping.", flush=True)
            return

    with conn.cursor() as cur:
        cur.execute("SELECT pid FROM patient_data ORDER BY pid LIMIT 50")
        pids = [row["pid"] for row in cur.fetchall()]

    if not pids:
        print("No patients found, skipping audit log seed.", flush=True)
        return

    # Pad pids list so slice arithmetic never runs out.
    while len(pids) < 50:
        pids = pids + pids

    today = datetime.utcnow().date()

    def ts(days_ago, hour, minute=0):
        return datetime.combine(today - timedelta(days=days_ago), dtime(hour, minute))

    # Four anchor offsets, each exactly 7 days apart → guaranteed distinct week buckets.
    # P1 = most recent, P4 = oldest (still within last 30 days).
    P1, P2, P3, P4 = 2, 9, 16, 23

    events = []   # (user, event_type, patient_id, datetime)

    # ── Period 1 ─────────────────────────────────────────────────────────────

    # dr_nguyen: 25 patient-access events, 12 unique patients → VOLUME_SPIKE
    for i in range(25):
        events.append(("dr_nguyen", "patient-record", pids[i % 12],
                       ts(P1, 9) + timedelta(minutes=i * 10)))

    # nurse_chen: after-hours access → OFF_HOURS
    for i in range(5):
        events.append(("nurse_chen", "patient-record", pids[i],
                       ts(P1, 21) + timedelta(minutes=i * 10)))

    # admin_hayes: 4 failed logins → CREDENTIAL_RISK (events 3-4 fire R-06)
    for i in range(4):
        events.append(("admin_hayes", "login-failure", None,
                       ts(P1, 22) + timedelta(minutes=i)))

    # ── Period 2 ─────────────────────────────────────────────────────────────

    # billing_ross: 25 patient-access events → VOLUME_SPIKE
    for i in range(25):
        events.append(("billing_ross", "patient-record", pids[10 + i % 12],
                       ts(P2, 10) + timedelta(minutes=i * 8)))

    # dr_patel: after-hours access at 3 am → OFF_HOURS
    for i in range(5):
        events.append(("dr_patel", "patient-record", pids[5 + i],
                       ts(P2, 3) + timedelta(minutes=i * 10)))

    # billing_ross: 4 failed logins → CREDENTIAL_RISK
    for i in range(4):
        events.append(("billing_ross", "login-failure", None,
                       ts(P2, 21) + timedelta(minutes=i)))

    # admin_hayes: admin action after hours → OFF_HOURS (R-10 + R-01)
    events.append(("admin_hayes", "security-administration-select", None, ts(P2, 22)))

    # ── Period 3 ─────────────────────────────────────────────────────────────

    # dr_patel: 25 patient-access events → VOLUME_SPIKE
    for i in range(25):
        events.append(("dr_patel", "patient-record", pids[20 + i % 12],
                       ts(P3, 9) + timedelta(minutes=i * 10)))

    # dr_nguyen: after-hours at 3 am → OFF_HOURS
    for i in range(5):
        events.append(("dr_nguyen", "patient-record", pids[3 + i],
                       ts(P3, 3) + timedelta(minutes=i * 10)))

    # nurse_chen: 4 failed logins → CREDENTIAL_RISK
    for i in range(4):
        events.append(("nurse_chen", "login-failure", None,
                       ts(P3, 21) + timedelta(minutes=i)))

    # ── Period 4 ─────────────────────────────────────────────────────────────

    # dr_nguyen: 25 patient-access events → VOLUME_SPIKE (different week from P1)
    for i in range(25):
        events.append(("dr_nguyen", "patient-record", pids[30 + i % 12],
                       ts(P4, 9) + timedelta(minutes=i * 10)))

    # admin_hayes: 4 failed logins → CREDENTIAL_RISK (different week from P1)
    for i in range(4):
        events.append(("admin_hayes", "login-failure", None,
                       ts(P4, 22) + timedelta(minutes=i)))

    # dr_patel: after-hours at 11 pm → OFF_HOURS (different week from P2)
    for i in range(5):
        events.append(("dr_patel", "patient-record", pids[8 + i],
                       ts(P4, 23) + timedelta(minutes=i * 10)))

    # nurse_chen: after-hours at 9:30 pm → OFF_HOURS (different week from P1/P3)
    for i in range(5):
        events.append(("nurse_chen", "patient-record", pids[2 + i],
                       ts(P4, 21, 30) + timedelta(minutes=i * 10)))

    # billing_ross: after-hours at 10 pm → OFF_HOURS (different week from P2)
    for i in range(5):
        events.append(("billing_ross", "patient-record", pids[15 + i],
                       ts(P4, 22) + timedelta(minutes=i * 10)))

    # ── Insert ────────────────────────────────────────────────────────────────

    insert_sql = "INSERT INTO log (date, event, user, patient_id) VALUES (%s, %s, %s, %s)"
    with conn.cursor() as cur:
        for user, event, patient_id, dt_val in events:
            cur.execute(insert_sql, (
                dt_val.strftime("%Y-%m-%d %H:%M:%S"),
                event,
                user,
                patient_id,
            ))
    conn.commit()
    print(f"Seeded {len(events)} demo audit log entries.", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    wait_for_db()
    conn = connect()
    try:
        facility_id = get_facility_id(conn)
        seed_facility(conn)
        seed_staff(conn, facility_id)
        seed_patients(conn, count=50)
        seed_audit_logs(conn)
        print("Seeding complete!", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
