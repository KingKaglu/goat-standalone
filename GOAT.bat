@echo off
title GOAT
rem GOAT is a native Python/Qt desktop app now - no Node server, no browser.
rem This just hands off to the real launcher (silent, single-instance aware).
start "" wscript.exe "%~dp0python\start-goat-app.vbs"
