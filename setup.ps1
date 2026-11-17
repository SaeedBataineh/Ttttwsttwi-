# Clipped Daily - one-shot setup for Windows.
# Run from the project folder in a normal (not admin) PowerShell:
#     .\setup.ps1
# Safe to run again any time; it skips what's already done.

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  WARN $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "  FAIL $msg" -ForegroundColor Red }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# ---------------------------------------------------------------- Python
Step "Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Warn "Python not found - installing Python 3.12 with winget"
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if (-not $ver) { Bad "Python still not available. Install it from python.org (tick 'Add to PATH') and rerun."; exit 1 }
if ([version]$ver -lt [version]"3.10") { Bad "Python $ver is too old (need 3.10+)."; exit 1 }
Ok "Python $ver"

# ---------------------------------------------------------------- venv + packages
Step "Virtual environment and packages"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    python -m venv .venv
    Ok "Created .venv"
} else { Ok ".venv exists" }

& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) { Bad "pip install failed - see errors above"; exit 1 }
Ok "Python packages installed"

# ---------------------------------------------------------------- ffmpeg
Step "ffmpeg"
$ff = & $venvPy -c "from clipped_daily.media.render import find_tool; print(find_tool('ffmpeg') or '')"
if (-not $ff) {
    Warn "ffmpeg not found - installing with winget"
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $ff = & $venvPy -c "from clipped_daily.media.render import find_tool; print(find_tool('ffmpeg') or '')"
}
if ($ff) { Ok "ffmpeg: $ff" } else { Bad "ffmpeg still missing. Close PowerShell, open a new one, and rerun .\setup.ps1" }

# ---------------------------------------------------------------- NVIDIA
Step "NVIDIA GPU"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>$null
    Ok "GPU: $gpu"
    Write-Host "  Installing CUDA libraries for GPU captions (~1 GB, first time only)..."
    & $venvPy -m pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"
    if ($LASTEXITCODE -eq 0) { Ok "CUDA libraries installed" } else { Warn "CUDA libraries failed to install - captions will use CPU" }
} else {
    Warn "nvidia-smi not found - install/update the NVIDIA driver. Everything still works on CPU."
}

# ---------------------------------------------------------------- .env
Step ".env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Ok "Created .env from .env.example"
} else { Ok ".env exists" }
$envText = Get-Content ".env" -Raw
foreach ($k in "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_REVIEW_CHANNEL_ID") {
    if ($envText -match "(?m)^$k=\s*\S+") { Ok "$k filled in" } else { Warn "$k is empty - open it with: notepad .env" }
}

# ---------------------------------------------------------------- doctor
Step "Full check (doctor)"
& $venvPy -m clipped_daily doctor

Write-Host "`nDone. Copy everything above (it contains no keys) and send it to Claude." -ForegroundColor Cyan
Write-Host "Next time, start with:  .venv\Scripts\activate" -ForegroundColor Cyan
