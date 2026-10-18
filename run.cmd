@echo off
rem Windows entry point: runs run.sh through Git Bash. Works from cmd,
rem PowerShell (.\run.cmd) or a double-click in Explorer.
rem
rem Plain `bash` is not used, because on most Windows machines it resolves to
rem C:\Windows\system32\bash.exe, which is WSL rather than Git Bash.

setlocal
set "GITBASH="

rem Git Bash sits next to git.exe: <git>\cmd\git.exe -> <git>\bin\bash.exe
for /f "delims=" %%G in ('where git 2^>nul') do (
  if not defined GITBASH if exist "%%~dpG..\bin\bash.exe" set "GITBASH=%%~dpG..\bin\bash.exe"
)
if not defined GITBASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GITBASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"

if not defined GITBASH (
  echo error: Git Bash not found. Install Git for Windows from https://git-scm.com
  pause
  exit /b 1
)

"%GITBASH%" "%~dp0run.sh" %*
set "CODE=%ERRORLEVEL%"

rem Keep the window open after a double-click so errors stay readable.
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b %CODE%
