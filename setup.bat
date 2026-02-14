@echo off
echo ============================================
echo   Nuvama Live News Monitor - Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo [1/3] Python found:
python --version
echo.

:: Install dependencies
echo [2/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
echo.

:: Install Playwright browsers
echo [3/3] Installing Playwright Chromium browser...
python -m playwright install chromium
if errorlevel 1 (
    echo ERROR: Failed to install Playwright browser.
    pause
    exit /b 1
)
echo.

:: Check .env file
if not exist .env (
    echo ============================================
    echo   IMPORTANT: Create your .env file!
    echo ============================================
    echo.
    echo Copy .env.example to .env and fill in your values:
    echo   copy .env.example .env
    echo.
    echo Then edit .env with your Telegram token and chat ID.
    echo.
) else (
    echo .env file found.
)

echo ============================================
echo   Setup Complete!
echo ============================================
echo.
echo To start the app, run: start.bat
echo Or run: python run_all.py
echo.
pause
