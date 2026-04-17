@echo off
set ROOT=%~dp0..
pushd "%ROOT%"
if "%~1"=="" (
  python -m launcher.app
) else (
  python -m core.app %*
)
set RC=%ERRORLEVEL%
popd
exit /b %RC%
