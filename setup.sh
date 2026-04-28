#!/usr/bin/env bash
# =============================================================================
# TrustPulse + OpenEMR - Full Environment Setup Script
# =============================================================================
# What this script does (step by step):
#   1. Checks that Docker and Docker Compose are installed
#   2. Copies the root .env if it doesn't exist yet
#   3. Starts the OpenEMR + MariaDB stack (root docker-compose.yml)
#   4. Waits for MariaDB and OpenEMR to become healthy
#   5. Creates the read-only TrustPulse DB user inside MariaDB
#   6. Copies trustpulse/.env.example → trustpulse/.env (if not present)
#   7. Starts the TrustPulse stack (trustpulse/docker-compose.yml)
#   8. Waits for TrustPulse to finish its startup ingestion cycle
#   9. Prints all access URLs and login credentials
#
# Run from the repo root:
#   chmod +x setup.sh && ./setup.sh
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}▶ $*${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# =============================================================================
# STEP 1 - Prerequisites check
# =============================================================================
header "Checking prerequisites"

command -v docker >/dev/null 2>&1 || error "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/"

# Support both 'docker compose' (v2 plugin) and 'docker-compose' (v1 standalone)
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    error "Docker Compose not found. Install Docker Desktop (includes Compose v2)."
fi

DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
success "Docker $DOCKER_VERSION found (Compose: $DC)"

# =============================================================================
# STEP 2 - Root environment file
# =============================================================================
header "Setting up root environment"

if [ ! -f ".env" ]; then
    warn ".env not found - using built-in defaults"
    cat > .env <<'ENVEOF'
TZ=America/New_York
MYSQL_ROOT_PASSWORD=rootpass
MYSQL_DATABASE=openemr
MYSQL_USER=openemr
MYSQL_PASSWORD=openemrpass
OPENEMR_ADMIN_USER=admin
OPENEMR_ADMIN_PASS=pass
OPENEMR_HTTP_PORT=8080
OPENEMR_HTTPS_PORT=8443
ENVEOF
    success "Created .env with defaults"
else
    success ".env already exists - using it as-is"
fi

# Load variables so we can pass them to docker exec later
# shellcheck source=.env
set -a; source .env; set +a

# =============================================================================
# STEP 3 - Start OpenEMR stack
# =============================================================================
header "Starting OpenEMR + MariaDB"

info "Running: $DC up -d"
$DC up -d
success "Containers started"

# =============================================================================
# STEP 4 - Wait for healthy containers
# =============================================================================
header "Waiting for MariaDB to become healthy"

TIMEOUT=180
ELAPSED=0
while true; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' openemr_mariadb 2>/dev/null || echo "missing")
    if [ "$STATUS" = "healthy" ]; then
        success "openemr_mariadb is healthy"
        break
    fi
    if [ $ELAPSED -ge $TIMEOUT ]; then
        error "MariaDB did not become healthy within ${TIMEOUT}s. Run: docker logs openemr_mariadb"
    fi
    echo -ne "\r  Waiting for MariaDB... ${ELAPSED}s (status: $STATUS)"
    sleep 5; ELAPSED=$((ELAPSED+5))
done

header "Waiting for OpenEMR web app to become healthy"
ELAPSED=0
while true; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' openemr_app 2>/dev/null || echo "missing")
    if [ "$STATUS" = "healthy" ]; then
        success "openemr_app is healthy"
        break
    fi
    if [ $ELAPSED -ge 300 ]; then
        warn "OpenEMR health check timed out - it may still be initialising. Continuing anyway."
        break
    fi
    echo -ne "\r  Waiting for OpenEMR... ${ELAPSED}s (status: $STATUS)"
    sleep 10; ELAPSED=$((ELAPSED+10))
done

# =============================================================================
# STEP 5 - Create read-only TrustPulse DB user
# =============================================================================
header "Creating TrustPulse read-only database user"

# Check if user already exists to make this idempotent
ALREADY=$(docker exec openemr_mariadb mariadb -u root -p"${MYSQL_ROOT_PASSWORD}" \
    -se "SELECT COUNT(*) FROM mysql.user WHERE User='trustpulse_ro';" 2>/dev/null || echo "0")

if [ "$ALREADY" -gt "0" ] 2>/dev/null; then
    success "trustpulse_ro user already exists - skipping SQL setup"
else
    docker exec -i openemr_mariadb mariadb -u root -p"${MYSQL_ROOT_PASSWORD}" openemr \
        < trustpulse/sql/openemr_ro_setup.sql
    success "Read-only user 'trustpulse_ro' created"
fi

# =============================================================================
# STEP 6 - TrustPulse environment file
# =============================================================================
header "Setting up TrustPulse environment"

if [ ! -f "trustpulse/.env" ]; then
    cp trustpulse/.env.example trustpulse/.env
    success "Created trustpulse/.env from example"
    warn "IMPORTANT: For any real deployment, change the secrets in trustpulse/.env:"
    warn "  TRUSTPULSE_JWT_SECRET"
    warn "  TRUSTPULSE_PATIENT_TOKEN_SECRET"
else
    success "trustpulse/.env already exists - using it as-is"
fi

# =============================================================================
# STEP 7 - Start TrustPulse
# =============================================================================
header "Starting TrustPulse"

info "Running: $DC -f trustpulse/docker-compose.yml up -d --build"
$DC -f trustpulse/docker-compose.yml up -d --build
success "TrustPulse container started"

# =============================================================================
# STEP 8 - Wait for TrustPulse API
# =============================================================================
header "Waiting for TrustPulse API to be ready"

ELAPSED=0
TIMEOUT=120
while true; do
    # Try to reach the login endpoint
    HTTP_CODE=$(docker exec trustpulse_app python3 -c \
        "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/api/auth/login'); print(r.status)" \
        2>/dev/null || echo "0")
    # 405 = endpoint exists but GET not allowed (login is POST-only) → server is up
    HTTP_CODE2=$(docker exec trustpulse_app python3 -c \
        "import urllib.request,urllib.error
try:
    urllib.request.urlopen('http://localhost:8000/docs')
    print('200')
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('0')" 2>/dev/null || echo "0")

    if [ "$HTTP_CODE2" = "200" ] || [ "$HTTP_CODE" = "200" ]; then
        success "TrustPulse API is ready"
        break
    fi
    if [ $ELAPSED -ge $TIMEOUT ]; then
        warn "TrustPulse did not respond within ${TIMEOUT}s - check: docker logs trustpulse_app"
        break
    fi
    echo -ne "\r  Waiting for TrustPulse API... ${ELAPSED}s"
    sleep 5; ELAPSED=$((ELAPSED+5))
done

# Extra wait for startup ingestion to complete
sleep 5

# =============================================================================
# STEP 9 - Done! Print access info
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Setup complete! Everything is running.${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "${BOLD}OpenEMR${RESET}"
echo -e "  Web UI (HTTP):    http://localhost:${OPENEMR_HTTP_PORT:-8080}"
echo -e "  Web UI (HTTPS):   https://localhost:${OPENEMR_HTTPS_PORT:-8443}"
echo -e "  Admin login:      admin / pass"
echo -e "  Doctor logins:    doctor1 / Doctor@2026  |  doctor2 / Doctor@2026"
echo ""
echo -e "${BOLD}TrustPulse${RESET}"
echo -e "  Dashboard:        http://localhost:8000"
echo -e "  API docs:         http://localhost:8000/docs"
echo ""
echo -e "${BOLD}TrustPulse accounts (all roles):${RESET}"
printf "  %-28s %-20s %s\n" "Email" "Password" "Role"
printf "  %-28s %-20s %s\n" "----------------------------" "--------------------" "----"
printf "  %-28s %-20s %s\n" "admin@trustpulse.local"      "TrustPulse@2026!"  "Admin (full access)"
printf "  %-28s %-20s %s\n" "compliance@trustpulse.local" "Comply@2026!"      "Compliance Officer"
printf "  %-28s %-20s %s\n" "auditor@trustpulse.local"    "Audit@2026!"       "Auditor (read-only)"
printf "  %-28s %-20s %s\n" "security@trustpulse.local"   "Secure@2026!"      "Security Admin"
echo ""
echo -e "${YELLOW}Next step:${RESET} Open http://localhost:8000 and log in."
echo -e "           Then browse OpenEMR to generate audit activity, and"
echo -e "           click 'Run Ingestion' in TrustPulse to see cases."
echo ""
echo -e "To stop everything:  docker compose down && docker compose -f trustpulse/docker-compose.yml down"
echo -e "Full guide:          trustpulse/SETUP.md"
echo ""
