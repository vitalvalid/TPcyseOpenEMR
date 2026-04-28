[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO]  $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK]    $Message" -ForegroundColor Green
}

function Write-WarnMsg {
    param([string]$Message)
    Write-Host "[WARN]  $Message" -ForegroundColor Yellow
}

function Throw-SetupError {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Write-Header {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-DockerCompose {
    param([string[]]$Args)
    & $script:DockerComposeExe @script:DockerComposeBaseArgs @Args
    if ($LASTEXITCODE -ne 0) {
        Throw-SetupError "Docker Compose command failed: $($Args -join ' ')"
    }
}

function Get-ComposeContainerId {
    param(
        [string]$ServiceName,
        [string[]]$ComposeArgs = @()
    )

    $containerId = & $script:DockerComposeExe @script:DockerComposeBaseArgs @ComposeArgs "ps" "-q" $ServiceName 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($containerId)) {
        return $null
    }

    return $containerId.Trim()
}

function Get-ComposeContainerName {
    param(
        [string]$ServiceName,
        [string[]]$ComposeArgs = @()
    )

    $containerId = Get-ComposeContainerId -ServiceName $ServiceName -ComposeArgs $ComposeArgs
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        return $null
    }

    $containerName = & docker inspect --format "{{.Name}}" $containerId 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($containerName)) {
        return $null
    }

    return $containerName.TrimStart("/")
}

function Get-ContainerHealth {
    param([string]$ContainerRef)

    if ([string]::IsNullOrWhiteSpace($ContainerRef)) {
        return "missing"
    }

    $status = & docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}" $ContainerRef 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($status)) {
        return "missing"
    }

    return $status.Trim()
}

function Wait-ForContainerHealth {
    param(
        [string]$ServiceName,
        [int]$TimeoutSeconds,
        [int]$SleepSeconds,
        [string]$DisplayName,
        [string[]]$ComposeArgs = @(),
        [bool]$WarnOnly = $false
    )

    $elapsed = 0
    while ($true) {
        $containerName = Get-ComposeContainerName -ServiceName $ServiceName -ComposeArgs $ComposeArgs
        $status = Get-ContainerHealth -ContainerRef $containerName
        if ($status -eq "healthy" -or $status -eq "no-healthcheck") {
            Write-Success "$DisplayName is healthy"
            return
        }

        if ($elapsed -ge $TimeoutSeconds) {
            if ($WarnOnly) {
                Write-WarnMsg "$DisplayName health check timed out after ${TimeoutSeconds}s. Continuing anyway."
                return
            }

            $logTarget = if ([string]::IsNullOrWhiteSpace($containerName)) { $ServiceName } else { $containerName }
            Throw-SetupError "$DisplayName did not become healthy within ${TimeoutSeconds}s. Run: docker logs $logTarget"
        }

        Write-Host ("`r  Waiting for {0}... {1}s (status: {2})" -f $DisplayName, $elapsed, $status) -NoNewline
        Start-Sleep -Seconds $SleepSeconds
        $elapsed += $SleepSeconds
    }
}

function Import-EnvFile {
    param([string]$Path)

    $values = @{}
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2) {
            continue
        }

        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        $values[$key] = $value
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }

    return $values
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Header "Checking prerequisites"

if (-not (Test-Command "docker")) {
    Throw-SetupError "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
}

if ((& docker compose version) 2>$null) {
    $script:DockerComposeExe = "docker"
    $script:DockerComposeBaseArgs = @("compose")
    $composeLabel = "docker compose"
} elseif (Test-Command "docker-compose") {
    $script:DockerComposeExe = "docker-compose"
    $script:DockerComposeBaseArgs = @()
    $composeLabel = "docker-compose"
} else {
    Throw-SetupError "Docker Compose not found. Install Docker Desktop (includes Compose v2)."
}

$dockerVersion = (& docker version --format "{{.Server.Version}}" 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($dockerVersion)) {
    $dockerVersion = "unknown"
}
Write-Success "Docker $dockerVersion found (Compose: $composeLabel)"

Write-Header "Setting up root environment"

$rootEnvPath = Join-Path $scriptDir ".env"
if (-not (Test-Path $rootEnvPath)) {
    Write-WarnMsg ".env not found - using built-in defaults"
    @"
TZ=America/New_York
MYSQL_ROOT_PASSWORD=rootpass
MYSQL_DATABASE=openemr
MYSQL_USER=openemr
MYSQL_PASSWORD=openemrpass
OPENEMR_ADMIN_USER=admin
OPENEMR_ADMIN_PASS=pass
OPENEMR_HTTP_PORT=8080
OPENEMR_HTTPS_PORT=8443
"@ | Set-Content -Path $rootEnvPath -Encoding ascii
    Write-Success "Created .env with defaults"
} else {
    Write-Success ".env already exists - using it as-is"
}

$envValues = Import-EnvFile -Path $rootEnvPath

Write-Header "Starting OpenEMR + MariaDB"
Write-Info "Running: $composeLabel up -d"
Invoke-DockerCompose -Args @("up", "-d")
Write-Success "Containers started"

Write-Header "Waiting for MariaDB to become healthy"
Wait-ForContainerHealth -ServiceName "mariadb" -TimeoutSeconds 180 -SleepSeconds 5 -DisplayName "MariaDB"
Write-Host ""

Write-Header "Waiting for OpenEMR web app to become healthy"
Wait-ForContainerHealth -ServiceName "openemr" -TimeoutSeconds 300 -SleepSeconds 10 -DisplayName "OpenEMR" -WarnOnly $true
Write-Host ""

Write-Header "Creating TrustPulse read-only database user"

$mysqlRootPassword = $envValues["MYSQL_ROOT_PASSWORD"]
$mariadbContainer = Get-ComposeContainerName -ServiceName "mariadb"
if ([string]::IsNullOrWhiteSpace($mariadbContainer)) {
    Throw-SetupError "Could not resolve the MariaDB container name from Docker Compose."
}

$userCount = & docker exec $mariadbContainer mariadb -u root "-p$mysqlRootPassword" -se "SELECT COUNT(*) FROM mysql.user WHERE User='trustpulse_ro';" 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($userCount)) {
    $userCount = "0"
}

if ([int]$userCount -gt 0) {
    Write-Success "trustpulse_ro user already exists - skipping SQL setup"
} else {
    $sqlPath = Join-Path $scriptDir "trustpulse/sql/openemr_ro_setup.sql"
    Get-Content -Path $sqlPath -Raw -Encoding utf8 | & docker exec -i $mariadbContainer mariadb -u root "-p$mysqlRootPassword" openemr
    if ($LASTEXITCODE -ne 0) {
        Throw-SetupError "Failed to create read-only TrustPulse database user."
    }
    Write-Success "Read-only user 'trustpulse_ro' created"
}

Write-Header "Running OpenEMR seed data"
Write-Info "Running: $composeLabel run --rm seed"
Invoke-DockerCompose -Args @("run", "--rm", "seed")
Write-Success "OpenEMR seed completed"

Write-Header "Setting up TrustPulse environment"

$trustpulseEnvPath = Join-Path $scriptDir "trustpulse/.env"
$trustpulseEnvExamplePath = Join-Path $scriptDir "trustpulse/.env.example"
if (-not (Test-Path $trustpulseEnvPath)) {
    Copy-Item -Path $trustpulseEnvExamplePath -Destination $trustpulseEnvPath
    Write-Success "Created trustpulse/.env from example"
    Write-WarnMsg "IMPORTANT: For any real deployment, change the secrets in trustpulse/.env:"
    Write-WarnMsg "  TRUSTPULSE_JWT_SECRET"
    Write-WarnMsg "  TRUSTPULSE_PATIENT_TOKEN_SECRET"
} else {
    Write-Success "trustpulse/.env already exists - using it as-is"
}

Write-Header "Starting TrustPulse"
Write-Info "Running: $composeLabel -f trustpulse/docker-compose.yml up -d --build"
Invoke-DockerCompose -Args @("-f", "trustpulse/docker-compose.yml", "up", "-d", "--build")
Write-Success "TrustPulse container started"

Write-Header "Waiting for TrustPulse API to be ready"

$trustpulseComposeArgs = @("-f", "trustpulse/docker-compose.yml")
$elapsed = 0
$timeout = 120
while ($true) {
    $trustpulseContainer = Get-ComposeContainerName -ServiceName "trustpulse" -ComposeArgs $trustpulseComposeArgs
    if ([string]::IsNullOrWhiteSpace($trustpulseContainer)) {
        $httpCode = "0"
        $httpCode2 = "0"
    } else {
        $httpCode = & docker exec $trustpulseContainer python3 -c "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/api/auth/login'); print(r.status)" 2>$null
        if ($LASTEXITCODE -ne 0) {
            $httpCode = "0"
        } else {
            $httpCode = $httpCode.Trim()
        }

        $httpCode2 = & docker exec $trustpulseContainer python3 -c "import urllib.request, urllib.error;`ntry:`n    urllib.request.urlopen('http://localhost:8000/docs'); print('200')`nexcept urllib.error.HTTPError as e:`n    print(e.code)`nexcept Exception:`n    print('0')" 2>$null
        if ($LASTEXITCODE -ne 0) {
            $httpCode2 = "0"
        } else {
            $httpCode2 = $httpCode2.Trim()
        }
    }

    if ($httpCode2 -eq "200" -or $httpCode -eq "200") {
        Write-Success "TrustPulse API is ready"
        break
    }

    if ($elapsed -ge $timeout) {
        Write-WarnMsg "TrustPulse did not respond within ${timeout}s - check: docker logs trustpulse_app"
        break
    }

    Write-Host ("`r  Waiting for TrustPulse API... {0}s" -f $elapsed) -NoNewline
    Start-Sleep -Seconds 5
    $elapsed += 5
}

Start-Sleep -Seconds 5
Write-Host ""
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Setup complete! Everything is running." -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "OpenEMR"
Write-Host "  Web UI (HTTP):    http://localhost:$($envValues['OPENEMR_HTTP_PORT'])"
Write-Host "  Web UI (HTTPS):   https://localhost:$($envValues['OPENEMR_HTTPS_PORT'])"
Write-Host "  Admin login:      admin / pass"
Write-Host "  Doctor logins:    doctor1 / Doctor@2026  |  doctor2 / Doctor@2026"
Write-Host ""
Write-Host "TrustPulse"
Write-Host "  Dashboard:        http://localhost:8000"
Write-Host "  API docs:         http://localhost:8000/docs"
Write-Host ""
Write-Host "TrustPulse accounts (all roles):"
Write-Host ("  {0,-28} {1,-20} {2}" -f "Email", "Password", "Role")
Write-Host ("  {0,-28} {1,-20} {2}" -f "----------------------------", "--------------------", "----")
Write-Host ("  {0,-28} {1,-20} {2}" -f "admin@trustpulse.local", "TrustPulse@2026!", "Admin (full access)")
Write-Host ("  {0,-28} {1,-20} {2}" -f "compliance@trustpulse.local", "Comply@2026!", "Compliance Officer")
Write-Host ("  {0,-28} {1,-20} {2}" -f "auditor@trustpulse.local", "Audit@2026!", "Auditor (read-only)")
Write-Host ("  {0,-28} {1,-20} {2}" -f "security@trustpulse.local", "Secure@2026!", "Security Admin")
Write-Host ""
Write-Host "Next step: Open http://localhost:8000 and log in."
Write-Host "           Then browse OpenEMR to generate audit activity, and"
Write-Host "           click 'Run Ingestion' in TrustPulse to see cases."
Write-Host ""
Write-Host "To stop everything:"
Write-Host "  docker compose down"
Write-Host "  docker compose -f trustpulse/docker-compose.yml down"
Write-Host "Full guide:          trustpulse/SETUP.md"
Write-Host ""
