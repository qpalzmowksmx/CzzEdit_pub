@echo off
setlocal DisableDelayedExpansion
rem Python-free Windows entry point. PowerShell is bundled with Windows.
if "%~1"=="--help" goto help
if "%~1"=="-h" goto help
if not "%~2"=="" goto usage
if not "%~1"=="" if not "%~1"=="--login" goto usage
set "CZZ_OPTION="
if "%~1"=="--login" set "CZZ_OPTION=-Login"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %CZZ_OPTION%
set "CZZ_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %CZZ_EXIT%
:usage
echo Usage: install.cmd [--login]
exit /b 2
:help
echo Usage: install.cmd [--login]
echo Python 3.10+ is reused; otherwise Python 3.13 is installed locally.
exit /b 0
