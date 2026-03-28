@echo off
echo ============================================
echo  Google Review Scraper API - Starting up...
echo ============================================
cd /d C:\google-review-api
call python -m uvicorn app:app --host 0.0.0.0 --port 5000 --timeout-keep-alive 120
pause
