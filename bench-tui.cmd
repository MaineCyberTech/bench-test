@echo off
setlocal EnableExtensions
rem bench-tui.cmd - portable launcher for the bench-test Windows console dashboard.
rem On first run it creates .venv and installs deps (numpy, psutil, windows-curses, PyTorch CUDA).
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "VENV=%REPO%\.venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo [bench] first run - creating venv and installing dependencies ...
  where python >nul 2>&1 || ( echo [bench] Python 3.10+ not found on PATH & exit /b 1 )
  python -m venv "%VENV%" || exit /b 1
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet numpy psutil windows-curses
  echo [bench] installing PyTorch CUDA 12.8 ... this is a one-time ~2.5 GB download
  "%VENV%\Scripts\python.exe" -m pip install --quiet --index-url https://download.pytorch.org/whl/cu128 torch
)

"%VENV%\Scripts\python.exe" "%REPO%\bench\tui.py" %*
