@echo off
setlocal
set "ROOT=%~dp0.."
set "OUT=%~dp0..\dist"
if not "%~1"=="" set "OUT=%~1"
set "CACHE=%ROOT%\build\pycache"
if defined PYTHON (
  set "PY=%PYTHON%"
) else (
  set "PY=python"
)
set "PYTHONPYCACHEPREFIX=%CACHE%"
pushd "%ROOT%"
"%PY%" -m pip wheel . --no-deps --no-build-isolation --wheel-dir "%OUT%"
set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
