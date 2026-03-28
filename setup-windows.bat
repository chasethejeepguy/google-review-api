@echo off
echo ============================================
echo  Google Review Scraper - First Time Setup
echo ============================================
cd /d C:\google-review-api
echo Installing Python packages...
pip install -r requirements.txt
echo.
echo Installing Chromium browser...
playwright install chromium
echo.
echo ============================================
echo  Setup complete! Run start-api.bat to start.
echo ============================================
pause
