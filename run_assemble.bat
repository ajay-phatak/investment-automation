@echo off
cd /d "%~dp0"
call C:\Users\wizar\.venvs\research-agent\Scripts\activate.bat
python thesis_research.py --assemble >> logs\assemble.log 2>&1
