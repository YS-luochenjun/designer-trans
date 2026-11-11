@echo off
setlocal
cd /d "%~dp0"
set "VT_ROOT=%~dp0"
set "VT_RUNTIME=%~dp0runtime"
set "PYTHONPATH=%~dp0src"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Run scripts/setup.py first.
  pause
  exit /b 1
)
echo Video Translation v2: http://127.0.0.1:8767
echo Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -X utf8 -B -m video_translation.interfaces.cli serve --port 8767
pause
