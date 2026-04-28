-- ─────────────────────────────────────────────────────────────────────────────
-- TrustPulse read-only user setup for a REAL OpenEMR database.
-- Run this as a MySQL/MariaDB admin user on your OpenEMR DB host.
--
-- WARNING: This creates a read-only user. TrustPulse never writes to OpenEMR.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Create the read-only user (change the password below before use)
CREATE USER IF NOT EXISTS 'trustpulse_ro'@'%' IDENTIFIED BY 'readonly123';

-- 2. Grant SELECT-only on the tables TrustPulse needs
GRANT SELECT ON openemr.log                           TO 'trustpulse_ro'@'%';
GRANT SELECT ON openemr.api_log                       TO 'trustpulse_ro'@'%';
GRANT SELECT ON openemr.users                         TO 'trustpulse_ro'@'%';
GRANT SELECT ON openemr.patient_data                  TO 'trustpulse_ro'@'%';
GRANT SELECT ON openemr.openemr_postcalendar_events   TO 'trustpulse_ro'@'%';

-- 3. Flush privileges
FLUSH PRIVILEGES;

-- ─────────────────────────────────────────────────────────────────────────────
-- After running this, set OPENEMR_DB_URL in TrustPulse's .env:
--
--   OPENEMR_DB_URL=mysql+pymysql://trustpulse_ro:readonly123@<host>:3306/openemr
--
-- To verify read-only posture:
--   SELECT user, grant_option FROM mysql.db
--   WHERE user='trustpulse_ro' AND db='openemr';
--
-- The user must have ONLY the SELECT privilege.
-- ─────────────────────────────────────────────────────────────────────────────
