# ShrinkRay installer: one-liner equivalent for Windows.
#
#   irm https://raw.githubusercontent.com/bennischober/ShrinkRay/main/install.ps1 | iex
#
# Idempotent: detects what exists (uv, ffmpeg, HandBrakeCLI, the repo) and
# only fills the gaps. Re-running is safe. No admin rights required beyond
# what winget itself asks for.

$ErrorActionPreference = "Stop"

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    $message" -ForegroundColor Green }
function Write-Skip($message) { Write-Host "    $message (already installed)" -ForegroundColor DarkGray }

$repoUrl = "https://github.com/bennischober/ShrinkRay"
$installDir = Join-Path $env:USERPROFILE ".shrinkray"

Write-Host ""
Write-Host "ShrinkRay installer" -ForegroundColor White
Write-Host "Catalog your movie library - then shrink it without losing quality."
Write-Host ""

# ── 1. winget (the package source for the binaries) ────────────────────
Write-Step "Checking winget"
if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Skip "winget"
} else {
    Write-Host "    winget not found - install it from the Microsoft Store"
    Write-Host "    ('App Installer' package), then re-run this script."
    exit 1
}

# ── 2. uv (Python + package management) ────────────────────────────────
Write-Step "Checking uv"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Skip "uv"
} else {
    Write-Host "    installing uv (winget)..."
    winget install --id astral-sh.uv --accept-source-agreements --accept-package-agreements
    # winget installs do not update the current session's PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
        [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Ok "uv installed"
    } else {
        Write-Host "    uv installed but not on PATH yet - open a NEW terminal and re-run this script." -ForegroundColor Yellow
        exit 1
    }
}

# ── 3. ffmpeg (GPL build: cropdetect + libvmaf needed) ─────────────────
Write-Step "Checking ffmpeg"
$ffmpegOk = $false
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    # verify it is a build with the filters we need
    $filters = & ffmpeg -hide_banner -filters 2>$null | Out-String
    if ($filters -match "cropdetect" -and $filters -match "libvmaf") {
        $ffmpegOk = $true
        Write-Skip "ffmpeg (cropdetect + libvmaf present)"
    } else {
        Write-Host "    ffmpeg found but MISSING cropdetect/libvmaf (LGPL build?)" -ForegroundColor Yellow
        Write-Host "    installing the GPL build (Gyan.FFmpeg)..."
    }
}
if (-not $ffmpegOk) {
    winget install --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
        [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    Write-Ok "ffmpeg (Gyan GPL build) installed"
}

# ── 4. HandBrakeCLI (the encoder) ───────────────────────────────────────
Write-Step "Checking HandBrakeCLI"
$handbrake = Get-Command HandBrakeCLI -ErrorAction SilentlyContinue
if (-not $handbrake) {
    foreach ($path in @(
        "$env:ProgramFiles\HandBrake\HandBrakeCLI.exe",
        "${env:ProgramFiles(x86)}\HandBrake\HandBrakeCLI.exe"
    )) {
        if (Test-Path $path) { $handbrake = $path; break }
    }
}
if ($handbrake) {
    Write-Skip "HandBrakeCLI"
} else {
    Write-Host "    installing HandBrake (winget)..."
    winget install --id HandBrake.HandBrake --accept-source-agreements --accept-package-agreements
    Write-Ok "HandBrake installed"
}

# ── 5. the repository ───────────────────────────────────────────────────
Write-Step "Checking the repository ($installDir)"
if (Test-Path (Join-Path $installDir ".git")) {
    Write-Skip "repository exists"
    Push-Location $installDir
    git pull --ff-only 2>$null | Out-Null
    Pop-Location
    Write-Ok "updated to latest main"
} else {
    git clone $repoUrl $installDir
    Write-Ok "cloned"
}

# ── 6. the Python environment ───────────────────────────────────────────
Write-Step "Syncing the Python environment"
Push-Location $installDir
uv sync --dev
Write-Ok "dependencies synced"

# ── 7. the web UI (optional: needs Node; CLI works without) ─────────────
Write-Step "Checking the web UI"
$uiDist = Join-Path $installDir "ui\dist\index.html"
if (Test-Path $uiDist) {
    Write-Skip "UI already built"
} elseif (Get-Command node -ErrorAction SilentlyContinue) {
    Write-Host "    building the UI (npm)..."
    Push-Location (Join-Path $installDir "ui")
    npm ci
    npm run build
    Pop-Location
    Write-Ok "UI built"
} else {
    Write-Host "    Node.js not found - skipping the UI build (the CLI works without it)." -ForegroundColor Yellow
    Write-Host "    For the dashboard: install Node (winget install OpenJS.NodeJS.LTS),"
    Write-Host "    then: cd $installDir\ui; npm ci; npm run build"
}

# ── done ────────────────────────────────────────────────────────────────
Pop-Location
Write-Host ""
Write-Host "ShrinkRay is ready." -ForegroundColor Green
Write-Host ""
Write-Host "  Quick start (open a NEW terminal for updated PATH):"
Write-Host "    cd $installDir"
Write-Host "    uv run shrinkray encode 'D:\Movies' --dry-run   # preview what would change"
Write-Host "    uv run shrinkray encode 'D:\Movies'             # shrink it (measured, bars removed)"
Write-Host "    uv run shrinkray serve                          # dashboard on http://127.0.0.1:8420"
Write-Host "    uv run shrinkray setup                          # configure, pause, resume"
Write-Host ""
