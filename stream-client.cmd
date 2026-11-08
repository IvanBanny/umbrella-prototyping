@echo off
setlocal
set "clientExe=%LOCALAPPDATA%\Programs\isaacsim-webrtc-streaming-client\Isaac Sim WebRTC Streaming Client.exe"
if not exist "%clientExe%" set "clientExe=%ProgramFiles%\Isaac Sim WebRTC Streaming Client\Isaac Sim WebRTC Streaming Client.exe"
if not "%~1"=="" set "clientExe=%~1"
if not exist "%clientExe%" (
    echo Isaac Sim WebRTC Streaming Client was not found.
    echo Install it first, or pass the full path to its executable as the first argument.
    exit /b 1
)
start "" "%clientExe%" --allow-loopback-in-peer-connection
