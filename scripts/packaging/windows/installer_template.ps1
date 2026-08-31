# CausaDB Windows Installer
$ErrorActionPreference = "Stop"

Write-Host "CausaDB Installer" -ForegroundColor Cyan
Write-Host "================="
Write-Host ""

# Check Python
$py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
  Write-Host "ERROR: $py not found. Install Python 3.10+ first." -ForegroundColor Red
  exit 1
}

# Check Python version
$pyVersion = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $pyVersion.Split('.')
$major = [int]$parts[0]
$minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
  Write-Host "ERROR: Python 3.10+ required, found $pyVersion" -ForegroundColor Red
  exit 1
}

# Home dir
$causadbHome = if ($env:CAUSADB_HOME) { $env:CAUSADB_HOME } else { "$env:USERPROFILE\.causadb" }
Write-Host "  Target: $py"
Write-Host "  Home:   $causadbHome"
Write-Host ""

# Create home
New-Item -ItemType Directory -Force -Path $causadbHome | Out-Null
New-Item -ItemType Directory -Force -Path "$causadbHome\ledgers" | Out-Null

# Install
Write-Host "Installing CausaDB..."
& $py -m pip install --user causadb
if (-not (Get-Command causadb -ErrorAction SilentlyContinue)) {
  Write-Host "pip install failed, trying local source..." -ForegroundColor Yellow
  $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  $projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
  & $py -m pip install --user -e $projectRoot
}

# Verify
if (-not (Get-Command causadb -ErrorAction SilentlyContinue)) {
  Write-Host "ERROR: causadb command not found after install" -ForegroundColor Red
  exit 1
}

# Config
$configPath = "$causadbHome\config.json"
if (-not (Test-Path $configPath)) {
  $config = @{ api_key = $null; ledgers_dir = "$causadbHome\ledgers" } | ConvertTo-Json
  $config | Out-File -FilePath $configPath -Encoding utf8
}

Write-Host ""
Write-Host "CausaDB installed: $(causadb --version 2>$null)" -ForegroundColor Green
Write-Host "Home: $causadbHome" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  causadb serve    # start daemon"
Write-Host "  causadb --help   # see all commands"
