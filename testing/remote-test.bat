@echo off
REM Wrapper around testing\harness.py.
REM
REM   remote-test.bat -env .\testing\harness.env -target linux -branch main up test hold
REM   remote-test.bat -env .\testing\harness.env -target linux logs down
REM
REM Every argument is passed through unchanged; see `remote-test.bat -h`.
setlocal

set "GITV_ROOT=%~dp0.."

REM Prefer the project venv so the harness runs against a known Python, but
REM fall back to whatever is on PATH -- the harness is stdlib-only precisely so
REM it can run before any venv exists.
set "PYTHON_CMD=%GITV_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_CMD%" set "PYTHON_CMD=python"

"%PYTHON_CMD%" "%GITV_ROOT%\testing\harness.py" %*
exit /b %ERRORLEVEL%
