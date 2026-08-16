@echo off
setlocal
set "ROOT=%~dp0.."
set "CACHE=%ROOT%\build\pycache"
if defined PYTHON (
  set "PY=%PYTHON%"
) else (
  set "PY=python"
)
set "PYTHONPYCACHEPREFIX=%CACHE%"
pushd "%ROOT%"
"%PY%" -m sekaisync %*
set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
