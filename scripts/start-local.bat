@echo off
title Blessing AI - Local Loopback Runtime
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
exit /b %ERRORLEVEL%
