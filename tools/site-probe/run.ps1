# Stock Watcher site probe launcher (Windows PowerShell).
# First run: creates .venv here, installs the backend's requirements and patchright's Chromium.
# Then forwards every argument to probe.py, e.g.
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 serve
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 sweep --only target,bestbuy
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Reqs = Join-Path $Here '..\..\backend\requirements.txt'
$Venv = Join-Path $Here '.venv'
$VPy  = Join-Path $Venv 'Scripts\python.exe'

function Find-Python {
    $candidates = New-Object System.Collections.ArrayList
    if ($env:PYTHON) { [void]$candidates.Add(@($env:PYTHON)) }
    foreach ($v in '3.13', '3.12', '3.11') { [void]$candidates.Add(@('py', "-$v")) }
    [void]$candidates.Add(@('python'))
    [void]$candidates.Add(@('python3'))
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $exe @pre -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return ,$c }
        } catch { continue }
    }
    return $null
}

if (-not (Test-Path $VPy)) {
    $py = Find-Python
    if (-not $py) {
        Write-Error 'Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").'
        exit 1
    }
    Write-Host "Creating virtual environment in $Venv ..."
    $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
    & $exe @pre -m venv $Venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Reinstall only when backend\requirements.txt changed.
$Stamp = Join-Path $Venv '.requirements.sha256'
$Want = (Get-FileHash -Algorithm SHA256 $Reqs).Hash
$Have = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { '' }
if ($Have -ne $Want) {
    Write-Host 'Installing backend requirements (one time, ~1-2 minutes) ...'
    & $VPy -m pip install --disable-pip-version-check -q --upgrade pip
    & $VPy -m pip install --disable-pip-version-check -q -r $Reqs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Set-Content -Path $Stamp -Value $Want
    Remove-Item -ErrorAction SilentlyContinue (Join-Path $Venv '.playwright-ok')
}

$PwOk = Join-Path $Venv '.playwright-ok'
if (-not (Test-Path $PwOk)) {
    # patchright and playwright are pinned to the same release: one Chromium serves both.
    $Engine = 'patchright'
    & $VPy -c 'import patchright' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $Engine = 'playwright' }
    Write-Host "Installing the Chromium used for bot-protected pages ($Engine, one time) ..."
    & $VPy -m $Engine install chromium
    if ($LASTEXITCODE -eq 0) { New-Item -ItemType File -Path $PwOk -Force | Out-Null }
    else { Write-Warning 'Chromium install failed; browser fallback will not work. You can still run with --no-browser.' }
}

& $VPy (Join-Path $Here 'probe.py') @args
exit $LASTEXITCODE
