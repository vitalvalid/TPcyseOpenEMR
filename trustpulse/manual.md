# TrustPulse Manual Setup

This guide is the fallback path when `setup.sh` or `setup.ps1` does not complete successfully.

It covers:

- Ubuntu or WSL2 Ubuntu shell
- Windows PowerShell with Docker Desktop
- manual OpenEMR startup
- manual OpenEMR seed execution
- manual TrustPulse database wiring
- manual TrustPulse startup and verification

Run all commands from the repository root unless stated otherwise.

## 1. Prerequisites

You need:

- Docker Desktop running
- Docker Compose available as `docker compose`
- at least one working shell:
  - Ubuntu in WSL2, or
  - Windows PowerShell

If you are using WSL2 Ubuntu, Docker Desktop must have WSL integration enabled for that distro.

In Docker Desktop:

1. Open `Settings`
2. Open `Resources`
3. Open `WSL Integration`
4. Enable `Ubuntu` or your target distro
5. Click `Apply & Restart`

## 2. Verify Docker

### Ubuntu or WSL2 Ubuntu

```bash
docker version
docker compose version
```

If `docker` is not found in WSL, restart WSL from Windows:

```cmd
wsl --shutdown
```

Then reopen Ubuntu and check again.

### Windows PowerShell

```powershell
docker version
docker compose version
```

If either command fails, fix Docker Desktop first before continuing.

## 3. Open the Repo

### Ubuntu or WSL2 Ubuntu

If the repo is on the Windows drive:

```bash
cd /mnt/c/Users/<your-user>/path/to/CYSEopenEMR-idea-development
```

You do not need `chmod +x setup.sh` to follow this manual process.

### Windows PowerShell

```powershell
cd C:\Users\<your-user>\path\to\CYSEopenEMR-idea-development
```

## 4. Create the Root `.env`

If `.env` does not already exist in the repo root, create it with these values:

```text
TZ=America/New_York
MYSQL_ROOT_PASSWORD=rootpass
MYSQL_DATABASE=openemr
MYSQL_USER=openemr
MYSQL_PASSWORD=openemrpass
OPENEMR_ADMIN_USER=admin
OPENEMR_ADMIN_PASS=pass
OPENEMR_HTTP_PORT=8080
OPENEMR_HTTPS_PORT=8443
```

## 5. Start OpenEMR and MariaDB

### Ubuntu or WSL2 Ubuntu

```bash
docker compose up -d
docker compose ps
```

### Windows PowerShell

```powershell
docker compose up -d
docker compose ps
```

You should see services for:

- `mariadb`
- `openemr`

If you want to see live startup errors:

### Ubuntu or WSL2 Ubuntu

```bash
docker compose logs mariadb
docker compose logs openemr
```

### Windows PowerShell

```powershell
docker compose logs mariadb
docker compose logs openemr
```

## 6. Wait for OpenEMR Stack to Be Ready

Check container health:

### Ubuntu or WSL2 Ubuntu

```bash
docker compose ps
```

### Windows PowerShell

```powershell
docker compose ps
```

Wait until:

- MariaDB is running and healthy
- OpenEMR is running and healthy, or at minimum stable enough to serve the login page

Optional verification:

### Ubuntu or WSL2 Ubuntu

```bash
curl -I http://localhost:8080
```

### Windows PowerShell

```powershell
curl.exe -I http://localhost:8080
```

## 7. Run the OpenEMR Seed Job

The `seed` service creates the demo users and patients needed by TrustPulse.

### Ubuntu or WSL2 Ubuntu

```bash
docker compose run --rm seed
```

### Windows PowerShell

```powershell
docker compose run --rm seed
```

This is idempotent and safe to rerun.

Expected seeded OpenEMR users:

- `admin / pass`
- `dr_nguyen / Doctor@2026`
- `dr_patel / Doctor@2026`
- `nurse_chen / Doctor@2026`
- `billing_ross / Doctor@2026`
- `admin_hayes / Doctor@2026`

## 8. Create the TrustPulse Read-Only Database User

TrustPulse reads OpenEMR data with a dedicated read-only MariaDB account.

First get the MariaDB container name if you are unsure:

### Ubuntu or WSL2 Ubuntu

```bash
docker compose ps
docker ps --format "{{.Names}}"
```

### Windows PowerShell

```powershell
docker compose ps
docker ps --format "{{.Names}}"
```

If your MariaDB container is named `openemr_mariadb`, run:

### Ubuntu or WSL2 Ubuntu

```bash
docker exec -i openemr_mariadb mariadb -u root -p"rootpass" openemr < trustpulse/sql/openemr_ro_setup.sql
```

### Windows PowerShell

```powershell
Get-Content .\trustpulse\sql\openemr_ro_setup.sql -Raw | docker exec -i openemr_mariadb mariadb -u root -prootpass openemr
```

If the MariaDB container has a different name, replace `openemr_mariadb` with the actual container name.

Optional verification:

### Ubuntu or WSL2 Ubuntu

```bash
docker exec openemr_mariadb mariadb -u root -p"rootpass" -e "SHOW GRANTS FOR 'trustpulse_ro'@'%';"
```

### Windows PowerShell

```powershell
docker exec openemr_mariadb mariadb -u root -prootpass -e "SHOW GRANTS FOR 'trustpulse_ro'@'%';"
```

## 9. Create `trustpulse/.env`

If `trustpulse/.env` does not exist, create it from the example:

### Ubuntu or WSL2 Ubuntu

```bash
cp trustpulse/.env.example trustpulse/.env
```

### Windows PowerShell

```powershell
Copy-Item .\trustpulse\.env.example .\trustpulse\.env
```

The default DB URL should usually remain:

```text
OPENEMR_DB_URL=mysql+pymysql://trustpulse_ro:readonly123@mariadb:3306/openemr
```

That hostname works because TrustPulse joins the same Docker network as the OpenEMR stack.

For any shared or non-lab deployment, change these secrets in `trustpulse/.env`:

- `TRUSTPULSE_JWT_SECRET`
- `TRUSTPULSE_PATIENT_TOKEN_SECRET`

## 10. Start TrustPulse

### Ubuntu or WSL2 Ubuntu

```bash
docker compose -f trustpulse/docker-compose.yml up -d --build
docker compose -f trustpulse/docker-compose.yml ps
```

### Windows PowerShell

```powershell
docker compose -f .\trustpulse\docker-compose.yml up -d --build
docker compose -f .\trustpulse\docker-compose.yml ps
```

## 11. Verify TrustPulse

Check logs:

### Ubuntu or WSL2 Ubuntu

```bash
docker logs trustpulse_app --tail 100
```

### Windows PowerShell

```powershell
docker logs trustpulse_app --tail 100
```

Look for application startup completion and successful ingestion behavior.

Then open:

- OpenEMR: `http://localhost:8080`
- TrustPulse: `http://localhost:8000`
- TrustPulse API docs: `http://localhost:8000/docs`

TrustPulse logins:

- `admin@trustpulse.local / TrustPulse@2026!`
- `compliance@trustpulse.local / Comply@2026!`
- `auditor@trustpulse.local / Audit@2026!`
- `security@trustpulse.local / Secure@2026!`

## 12. Generate Audit Activity

TrustPulse does not create cases from static seed data alone. You need new OpenEMR activity.

Use one of these non-admin OpenEMR users:

- `dr_nguyen`
- `dr_patel`
- `nurse_chen`
- `billing_ross`
- `admin_hayes`

Recommended flow:

1. Log in to OpenEMR
2. Open `Patient Search -> All Patients`
3. Open several patient charts
4. Return to TrustPulse
5. Click `Run Ingestion`

Do not use `admin` if you expect new TrustPulse events. The connector intentionally skips `admin` activity in this environment.

## 13. Manual Recovery Commands

If OpenEMR is already running but TrustPulse is not:

### Ubuntu or WSL2 Ubuntu

```bash
docker compose run --rm seed
docker exec -i openemr_mariadb mariadb -u root -p"rootpass" openemr < trustpulse/sql/openemr_ro_setup.sql
cp trustpulse/.env.example trustpulse/.env
docker compose -f trustpulse/docker-compose.yml up -d --build
```

### Windows PowerShell

```powershell
docker compose run --rm seed
Get-Content .\trustpulse\sql\openemr_ro_setup.sql -Raw | docker exec -i openemr_mariadb mariadb -u root -prootpass openemr
Copy-Item .\trustpulse\.env.example .\trustpulse\.env
docker compose -f .\trustpulse\docker-compose.yml up -d --build
```

## 14. Shutdown Commands

### Ubuntu or WSL2 Ubuntu

```bash
docker compose down
docker compose -f trustpulse/docker-compose.yml down
```

### Windows PowerShell

```powershell
docker compose down
docker compose -f .\trustpulse\docker-compose.yml down
```

To also remove volumes:

### Ubuntu or WSL2 Ubuntu

```bash
docker compose down -v
docker compose -f trustpulse/docker-compose.yml down -v
```

### Windows PowerShell

```powershell
docker compose down -v
docker compose -f .\trustpulse\docker-compose.yml down -v
```

## 15. Troubleshooting

- If `setup.sh` fails in WSL because `docker` is missing, enable WSL integration in Docker Desktop and restart WSL with `wsl --shutdown`.
- If `setup.ps1` fails while waiting for MariaDB, run the manual sequence above instead of relying on the script.
- If `trustpulse_app` starts but cannot connect to MariaDB, confirm the read-only user was created and `trustpulse/.env` still points to `mariadb:3306`.
- If the seed did not run, execute `docker compose run --rm seed` manually.
- If TrustPulse shows no new cases, generate fresh OpenEMR activity with a non-admin account and then run ingestion again.
