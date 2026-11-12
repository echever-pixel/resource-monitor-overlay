@echo off
rem Starts the overlay. Installs psutil the first time.
cd /d "%~dp0"
python -c "import psutil" 2>nul || python -m pip install --user psutil
start "" pythonw "%~dp0resmon.pyw"
