@echo off
setlocal EnableExtensions
rem bench-gui.cmd - portable launcher for the bench-test Windows GUI dashboard.
rem On first run it creates .venv and installs deps (numpy, psutil, PyTorch CUDA).
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "VENV=%REPO%\.venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo [bench] first run - creating venv and installing dependencies ...
  where python >nul 2>&1 || ( echo [bench] Python 3.10+ not found on PATH & exit /b 1 )
  python -m venv "%VENV%" || exit /b 1
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet numpy psutil
  echo [bench] installing PyTorch CUDA 12.8 ... this is a one-time ~2.5 GB download
  "%VENV%\Scripts\python.exe" -m pip install --quiet --index-url https://download.pytorch.org/whl/cu128 torch
)

rem The venv is reused across updates, so check the deps are actually there: one
rem created before psutil was added would record no CPU/RAM/disk/net telemetry.
"%VENV%\Scripts\python.exe" -c "import numpy, psutil" >nul 2>&1
if errorlevel 1 (
  echo [bench] installing missing dependencies: numpy psutil ...
  "%VENV%\Scripts\python.exe" -m pip install --quiet numpy psutil
)
"%VENV%\Scripts\python.exe" -c "import torch" >nul 2>&1
if errorlevel 1 (
  echo [bench] PyTorch missing - the llm/gfx phases need it, a one-time ~2.5 GB download ...
  "%VENV%\Scripts\python.exe" -m pip install --quiet --index-url https://download.pytorch.org/whl/cu128 torch
)

"%VENV%\Scripts\python.exe" "%REPO%\bench\gui.py" %*
