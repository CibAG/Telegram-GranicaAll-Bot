@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
python -u bot.py
