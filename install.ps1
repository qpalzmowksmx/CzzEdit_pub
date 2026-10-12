# Invoked by install.cmd. No administrator rights or persistent PATH edits.
# https://docs.astral.sh/uv/reference/installer/
param([switch]$Login)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot
$runtimeDir = Join-Path $PSScriptRoot '.czzedit\runtime'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$env:UV_UNMANAGED_INSTALL = Join-Path $runtimeDir 'uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimeDir 'python'
$env:UV_CACHE_DIR = Join-Path $runtimeDir 'cache'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_NO_CONFIG = '1'
$env:PYTHONNOUSERSITE = '1'
Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
$uvExe = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
$installerPath = $null
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Executable" }
}
function Ensure-Uv {
    if (Test-Path -LiteralPath $uvExe) { return }
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $script:installerPath = Join-Path $runtimeDir ('uv-installer-' + [guid]::NewGuid().ToString('N') + '.ps1')
    Write-Host 'Downloading the project-local Python installer (uv)...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $script:installerPath
    & $script:installerPath
    if (-not (Test-Path -LiteralPath $uvExe)) { throw 'uv installation failed.' }
}
try {
    if (-not (Test-Path 'czzedit\__main__.py')) { throw 'Keep these scripts in the project folder.' }
    if (-not (Test-Path '.venv')) {
        $pythonExe = $null
        foreach ($candidate in @('py','python','python3')) {
            $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -eq $command) { continue }
            # Skip Windows Store aliases; uv can discover real registered installs.
            if ($command.Source -like '*\Microsoft\WindowsApps\*') { continue }
            $probe = @('-E','-s','-c','import sys,sqlite3; assert sys.version_info >= (3,10); print(sys.executable)')
            if ($candidate -eq 'py') { $probe = @('-3') + $probe }
            try {
                $found = & $command.Source @probe 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $pythonExe = ([string]$found).Trim(); break }
            } catch { continue }
        }
        if ($pythonExe) {
            Write-Host "Reusing Python: $pythonExe"
            Invoke-Checked $pythonExe @('-E','-s','-m','venv','--without-pip','.venv')
        } else {
            Ensure-Uv
            try {
                $found = & $uvExe python find --system --no-managed-python --no-python-downloads '>=3.10' 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $pythonExe = ([string]$found).Trim() }
            } catch { $pythonExe = $null }
            if (-not $pythonExe) {
                Write-Host 'No compatible Python found. Installing Python 3.13 for this OS/architecture...'
                Invoke-Checked $uvExe @('python','install','3.13','--no-bin')
                $pythonExe = (& $uvExe python find --system --managed-python --no-python-downloads 3.13)
                if ($LASTEXITCODE -ne 0) { throw 'Could not locate the installed Python.' }
                $pythonExe = ([string]$pythonExe).Trim()
            }
            Invoke-Checked $uvExe @('venv','--python',$pythonExe,'--no-python-downloads','.venv')
        }
    }
    $validateCode = "import sys,sqlite3; from pathlib import Path; assert sys.version_info >= (3,10); assert Path(sys.prefix).resolve() == Path('.venv').resolve(); assert sys.prefix != sys.base_prefix; assert 'include-system-site-packages = false' in Path('.venv/pyvenv.cfg').read_text().lower()"
    Invoke-Checked $venvPython @('-E','-s','-c',$validateCode)
    Ensure-Uv
    Invoke-Checked $uvExe @('pip','install','--python',$venvPython,'-r','requirements.txt')
    if ($Login) {
        Ensure-Uv
        Invoke-Checked $uvExe @('pip','install','--python',$venvPython,'-r','requirements-login.txt')
        Invoke-Checked $venvPython @('-E','-s','-m','playwright','install','chromium')
    }
    Invoke-Checked $venvPython @('-E','-s','-m','czzedit','--help')
    Write-Host 'CzzEdit is ready. Start it with run.cmd.'
    exit 0
} catch {
    Write-Host ('Installation failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Check your network and folder permissions. If .venv is incomplete or incompatible, rename it and retry. Do not remove .czzedit: it contains saved chats and the local Python runtime.'
    exit 1
} finally {
    if ($installerPath -and (Test-Path -LiteralPath $installerPath)) { Remove-Item -LiteralPath $installerPath -Force }
}
