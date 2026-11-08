@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0simproto" %*
    exit /b
)
python "%~dp0simproto" %*
exit /b %errorlevel%
