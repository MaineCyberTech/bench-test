@echo off
rem bench-agent.cmd - start the bench-test control-plane agent on Windows.
rem
rem   set BENCHD_SERVER=http://lab:8722
rem   set BENCHD_TOKEN=...
rem   bench-agent.cmd
rem
rem Uses the repo .venv if present, else the python on PATH.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0bench\agent.py" %*
