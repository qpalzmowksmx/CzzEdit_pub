@echo off
setlocal DisableDelayedExpansion
rem No arguments: open the UI. Other arguments are forwarded to CzzEdit.
set "CZZ_EXIT=1"
pushd "%~dp0"
if errorlevel 1 goto end
".venv\Scripts\python.exe" -E -s -c "import sys; from pathlib import Path; assert sys.version_info >= (3,10); assert sys.prefix != sys.base_prefix; assert Path(sys.prefix).resolve() == Path('.venv').resolve(); assert 'include-system-site-packages = false' in Path('.venv/pyvenv.cfg').read_text().lower()" >nul 2>&1
if errorlevel 1 (
  echo Run install.cmd first to prepare Python for this project.
  goto finish
)
".venv\Scripts\python.exe" -E -s -m czzedit %*
set "CZZ_EXIT=%ERRORLEVEL%"
:finish
popd
:end
if "%~1"=="" pause
exit /b %CZZ_EXIT%
