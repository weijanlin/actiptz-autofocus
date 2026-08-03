@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  set "PYTHON_EXE=C:\Users\weijan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
  "%PYTHON_EXE%" -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
start "ACTi Auto HOI" http://127.0.0.1:8087
.venv\Scripts\python.exe app.py
