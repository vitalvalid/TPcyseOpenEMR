# TrustPulse - Full Setup Guide

This guide explains every step that `setup.sh` and `setup.ps1` perform, so you can run them manually if needed, understand what is happening, or troubleshoot failures.

---

## Requirements

### System

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| RAM | 4 GB | 8 GB recommended for smooth Docker performance |
| Disk | 5 GB free | OpenEMR image + MariaDB data + TrustPulse |
| OS | Linux / macOS / Windows | On Windows, use either PowerShell (`setup.ps1`) or WSL2 |

### Software

| Software | Version | How to Install |
|----------|---------|---------------|
| Docker Engine | 24+ | https://docs.docker.com/engine/install/ |
| Docker Compose | v2 (plugin) or v1 (standalone) | Bundled with Docker Desktop; or `apt install docker-compose-plugin` |
| Git | any | https://git-scm.com/downloads |
| Bash | any | Needed for `setup.sh`; Windows PowerShell can use `setup.ps1` instead |

> **No Python or Node.js** needed on the host. All application code runs inside Docker containers.

For automated setup:

- Linux/macOS/WSL2: `./setup.sh`
- Windows PowerShell: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`

If you want a Windows `.exe` wrapper for the PowerShell script:

```powershell
Install-Module ps2exe -Scope CurrentUser
Invoke-PS2EXE .\setup.ps1 .\setup.exe
```

Verify your installs:

```bash
docker --version          # e.g. Docker version 25.0.3
docker compose version    # e.g. Docker Compose version v2.24.5
```

---

## Step 0 - Clone the Repository

```bash
git clone <repo-url>
cd cyseOpenEMR
```

Everything below is run from the `cyseOpenEMR/` repo root unless stated otherwise.

---

## Step 1 - Create the Root `.env` File

The root `.env` controls OpenEMR's ports and database credentials.

```bash
cat > .env <<'EOF'
TZ=America/New_York
MYSQL_ROOT_PASSWORD=rootpass
MYSQL_DATABASE=openemr
MYSQL_USER=openemr
MYSQL_PASSWORD=openemrpass
OPENEMR_ADMIN_USER=admin
OPENEMR_ADMIN_PASS=pass
OPENEMR_HTTP_PORT=8080
OPENEMR_HTTPS_PORT=8443
EOF
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `MYSQL_ROOT_PASSWORD` | rootpass | MariaDB root password (needed to create the read-only user later) |
| `MYSQL_USER` / `MYSQL_PASSWORD` | openemr / openemrpass | OpenEMR application DB credentials |
| `OPENEMR_ADMIN_USER` / `OPENEMR_ADMIN_PASS` | admin / pass | OpenEMR web admin login |
| `OPENEMR_HTTP_PORT` | 8080 | Host port for HTTP access |
| `OPENEMR_HTTPS_PORT` | 8443 | Host port for HTTPS access |

---

## Step 2 - Start OpenEMR + MariaDB

```bash
docker compose up -d
```

This starts two containers:
- `openemr_mariadb` - MariaDB 10.11 database with synthetic patient data
- `openemr_app` - OpenEMR 7.0.2 web application

**Wait for both containers to become healthy** before continuing:

```bash
docker ps
# Both containers should show "(healthy)" in the STATUS column
# This can take 2–5 minutes on first run (image download + DB initialisation)
```

You can stream logs while waiting:

```bash
docker logs openemr_mariadb -f   # watch database startup
docker logs openemr_app -f       # watch OpenEMR startup
```

OpenEMR is ready when you see it return HTML at:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080
# Should print 200
```

---

## Step 3 - Create the Read-Only TrustPulse Database User

TrustPulse needs read-only access to OpenEMR's database. This creates a dedicated `trustpulse_ro` user with SELECT-only permissions:

```bash
docker exec -i openemr_mariadb mariadb -u root -p"rootpass" openemr \
  < trustpulse/sql/openemr_ro_setup.sql
```

> Replace `rootpass` with whatever you set for `MYSQL_ROOT_PASSWORD` in `.env`.

The SQL file grants SELECT on: `log`, `api_log`, `users`, `patient_data`, `openemr_postcalendar_events`. Nothing else - no INSERT, UPDATE, or DELETE.

To verify the grants:

```bash
docker exec openemr_mariadb mariadb -u root -p"rootpass" \
  -e "SHOW GRANTS FOR 'trustpulse_ro'@'%';"
# Should show only: GRANT SELECT ON `openemr`.`log` TO ...  (and the other tables)
```

This step is **idempotent** - safe to run again if something went wrong.

---

## Step 4 - Create the TrustPulse `.env` File

```bash
cp trustpulse/.env.example trustpulse/.env
```

Open `trustpulse/.env` and review the values. For a lab environment the defaults work fine. For any shared or internet-accessible deployment, change these two secrets:

| Variable | What to Change |
|----------|---------------|
| `TRUSTPULSE_JWT_SECRET` | Any long random string (used to sign login tokens) |
| `TRUSTPULSE_PATIENT_TOKEN_SECRET` | Any long random string (used to hash patient IDs in evidence exports) |

The other variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENEMR_DB_URL` | mysql+pymysql://trustpulse_ro:readonly123@mariadb:3306/openemr | Read-only connection to OpenEMR's MariaDB |
| `TRUSTPULSE_DB_URL` | sqlite:////app/data/trustpulse.db | TrustPulse internal database (SQLite) |
| `TRUSTPULSE_ADMIN_EMAIL` | admin@trustpulse.local | Bootstrap admin account |
| `TRUSTPULSE_ADMIN_PASSWORD` | TrustPulse@2026! | Bootstrap admin password |
| `CLINIC_NAME` | Demo Clinic | Shown in the UI and evidence exports |
| `INGESTION_INTERVAL_SECONDS` | 60 | How often to auto-pull OpenEMR logs |

---

## Step 5 - Build and Start TrustPulse

```bash
docker compose -f trustpulse/docker-compose.yml up -d --build
```

The `--build` flag rebuilds the Python image from `trustpulse/backend/Dockerfile`. On first run this installs all Python dependencies (takes ~1–2 minutes).

TrustPulse joins the same Docker network as OpenEMR (`cyseopenemr_default`) so it can reach MariaDB at the hostname `mariadb`.

Check that it started:

```bash
docker logs trustpulse_app -f
# Look for: "Application startup complete."
# And: "Ingestion cycle complete"
```

---

## Step 6 - Verify Everything Is Running

```bash
docker ps
```

You should see three containers running:

| Container | Port | Status |
|-----------|------|--------|
| openemr_mariadb | (internal) | healthy |
| openemr_app | 8080, 8443 | healthy |
| trustpulse_app | 8000 | running |

Test the TrustPulse API:

```bash
curl http://localhost:8000/docs
# Should return 200 (Swagger UI)
```

---

## Access URLs

| Service | URL |
|---------|-----|
| TrustPulse Dashboard | http://localhost:8000 |
| TrustPulse API Docs | http://localhost:8000/docs |
| OpenEMR (HTTP) | http://localhost:8080 |
| OpenEMR (HTTPS) | https://localhost:8443 |

---

## Login Accounts

### OpenEMR

| Username | Password | Role |
|----------|----------|------|
| admin | pass | System administrator |
| doctor1 | Doctor@2026 | Physician |
| doctor2 | Doctor@2026 | Physician |

### TrustPulse

| Email | Password | Role |
|-------|----------|------|
| admin@trustpulse.local | TrustPulse@2026! | Admin (full access) |
| compliance@trustpulse.local | Comply@2026! | Compliance Officer |
| auditor@trustpulse.local | Audit@2026! | Auditor (read-only) |
| security@trustpulse.local | Secure@2026! | Security Admin |

---

## Generating Activity to Review

TrustPulse reads OpenEMR's audit logs. With a fresh install there is no activity yet. To generate cases:

1. Open **http://localhost:8080** and log in as `admin` / `pass`
2. Go to **Patient Search → All Patients** - open 5–10 patient charts quickly
3. Log out, then log in as `doctor1` / `Doctor@2026` and access several different patient records
4. In a private/incognito window, try logging in with a wrong password 3–5 times
5. Open **http://localhost:8000**, log in as the compliance user, and click **Run Ingestion**
6. Cases will appear in the Review Queue

---

## Stopping and Cleaning Up

**Stop without deleting data:**

```bash
docker compose down
docker compose -f trustpulse/docker-compose.yml down
```

**Stop and delete all data** (OpenEMR patients, TrustPulse cases - full reset):

```bash
docker compose down -v
docker compose -f trustpulse/docker-compose.yml down -v
```

**Restart from scratch** (also removes cached images):

```bash
docker compose down -v --remove-orphans
docker container prune -f
docker volume prune -f
docker image prune -a -f
docker compose up -d
```

---

## Troubleshooting

### `setup.sh` fails at "Waiting for MariaDB"

MariaDB can take 3–5 minutes on first run while it initialises the OpenEMR schema. Wait and retry, or check:

```bash
docker logs openemr_mariadb --tail 30
```

### Port 8080 or 8000 already in use

Change the port in `.env`:

```
OPENEMR_HTTP_PORT=8081
```

Or stop whatever is using the port:

```bash
sudo lsof -i :8080
```

### TrustPulse can't connect to OpenEMR DB

Check the `trustpulse_ro` user was created (Step 3). Also confirm both containers are on the same Docker network:

```bash
docker network inspect cyseopenemr_default
# Both openemr_mariadb and trustpulse_app should appear under "Containers"
```

### "Permission denied" running setup.sh

```bash
chmod +x setup.sh
./setup.sh
```

### Docker not found on Windows

Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and enable the WSL2 backend. Then run all commands inside a WSL2 terminal (Ubuntu).

---

## Running the Tests (Optional)

Tests run inside the container, no local Python needed:

```bash
docker exec trustpulse_app pytest tests/ -v
```

Or locally if you have Python 3.11+:

```bash
cd trustpulse
pip install -r backend/requirements.txt
pytest tests/ -v
```

Tests use an in-memory SQLite database - no OpenEMR connection required.

---

## Python Dependencies (backend/requirements.txt)

These are installed automatically inside the Docker image during build:

| Package | Version | Purpose |
|---------|---------|---------|
| fastapi | 0.111.0 | Web framework for the API |
| uvicorn[standard] | 0.29.0 | ASGI server |
| sqlalchemy | 2.0.30 | ORM for TrustPulse internal DB |
| pymysql | 1.1.1 | MySQL/MariaDB driver (for OpenEMR read-only connection) |
| cryptography | 42.0.7 | Required by PyMySQL for TLS |
| pydantic | 2.7.1 | Request/response validation |
| python-multipart | 0.0.9 | Form data parsing |
| numpy | 1.26.4 | Statistical baseline calculations |
| python-dateutil | 2.9.0 | Date parsing for audit log timestamps |
| aiofiles | 23.2.1 | Async file I/O |
| jinja2 | 3.1.4 | Evidence report HTML templating |
| python-jose[cryptography] | 3.3.0 | JWT creation and verification |
| passlib[bcrypt] | 1.7.4 | Password hashing |
| bcrypt | 4.0.1 | bcrypt backend for passlib |
| httpx | 0.27.0 | HTTP client (used in tests) |
| pytest | 8.2.0 | Test runner |
| pytest-asyncio | 0.23.6 | Async test support |
