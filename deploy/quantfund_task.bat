@echo off
rem QuantFund daily advisor - entry for Windows Task Scheduler / desktop shortcut
rem All comments kept ASCII on purpose (CRLF + codepage safe).
rem No args = auto slot detection; optional args: pre / mid / post
rem 2026-09-08 migrated WSL -> Windows native. Runs the project-local venv
rem (Python 3.12, deps pinned in requirements.txt to match the old WSL env).
rem %~dp0 keeps this portable across drives as long as deploy\ stays under project root.
setlocal
set "PROJ=%~dp0.."
set "PY=%PROJ%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
cd /d "%PROJ%"
"%PY%" run.py %*
endlocal
