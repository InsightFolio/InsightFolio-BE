# PowerShell script to install all backend dependencies for InsightFolio-BE
# Run this script to set up the complete backend environment

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  InsightFolio-BE Setup Installer" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# Check if Python is installed
Write-Host "Checking Python installation..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    Write-Host "✓ Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ Python is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Python 3.11 or higher from https://www.python.org" -ForegroundColor Red
    exit 1
}

# Check if pip is installed
Write-Host "`nChecking pip installation..." -ForegroundColor Yellow
try {
    $pipVersion = python -m pip --version 2>&1
    Write-Host "✓ Found: $pipVersion" -ForegroundColor Green
} catch {
    Write-Host "✗ pip is not installed" -ForegroundColor Red
    Write-Host "Installing pip..." -ForegroundColor Yellow
    python -m ensurepip --upgrade
}

# Upgrade pip
Write-Host "`nUpgrading pip, setuptools, and wheel..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel

# Install all backend dependencies
Write-Host "`n======================================" -ForegroundColor Cyan
Write-Host "  Installing Backend Libraries" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# Core Flask and web framework
Write-Host "Installing Flask and web framework..." -ForegroundColor Yellow
python -m pip install Flask==3.1.2 Flask-Bcrypt==1.0.1 flask-cors==6.0.1 Flask-JWT-Extended==4.7.1 Flask-SQLAlchemy==3.1.1 Flask-Migrate==4.1.0

# Database drivers and ORMs
Write-Host "`nInstalling database libraries..." -ForegroundColor Yellow
python -m pip install PyMySQL==1.1.2 pymongo==4.10.1 mongoengine==0.29.1 SQLAlchemy==2.0.44

# Data science and ML libraries
Write-Host "`nInstalling data science and ML libraries..." -ForegroundColor Yellow
python -m pip install numpy==1.26.4 pandas==2.2.3 scikit-learn==1.6.0 lightgbm==4.3.0

# Financial data and Qlib
Write-Host "`nInstalling financial libraries..." -ForegroundColor Yellow
python -m pip install yfinance==0.2.66 pyqlib==0.9.7

# Security and authentication
Write-Host "`nInstalling security libraries..." -ForegroundColor Yellow
python -m pip install bcrypt==5.0.0 cryptography==46.0.3 PyJWT==2.10.1

# Utilities
Write-Host "`nInstalling utility libraries..." -ForegroundColor Yellow
python -m pip install python-dotenv==1.2.1 requests==2.32.3 click==8.3.0 colorama==0.4.6

# Testing framework
Write-Host "`nInstalling testing framework..." -ForegroundColor Yellow
python -m pip install pytest==8.3.4

# Additional dependencies
Write-Host "`nInstalling additional dependencies..." -ForegroundColor Yellow
python -m pip install blinker==1.9.0 cffi==2.0.0 greenlet==3.2.4 itsdangerous==2.2.0 Jinja2==3.1.6 MarkupSafe==3.0.3 pycparser==2.23 typing_extensions==4.15.0 Werkzeug==3.1.3

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✓ All backend libraries installed successfully!" -ForegroundColor Green
} else {
    Write-Host "`n✗ Some packages failed to install" -ForegroundColor Red
    Write-Host "Check the error messages above for details" -ForegroundColor Yellow
}

Write-Host "`n======================================" -ForegroundColor Cyan
Write-Host "  Installation Complete" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

# Verify critical packages
Write-Host "`nVerifying critical packages:" -ForegroundColor Yellow
$criticalPackages = @("Flask", "pymongo", "mongoengine", "PyMySQL", "SQLAlchemy", "pandas", "numpy", "yfinance", "pytest", "qlib")
foreach ($package in $criticalPackages) {
    $installed = python -m pip show $package 2>$null
    if ($installed) {
        Write-Host "  ✓ $package" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $package (missing)" -ForegroundColor Red
    }
}

Write-Host "`n✓ Backend setup complete! Next steps:" -ForegroundColor Green
Write-Host "  1. Create a .env file in flask_app/ with your environment variables" -ForegroundColor Cyan
Write-Host "  2. Run the Flask app:" -ForegroundColor Cyan
Write-Host "     cd flask_app" -ForegroundColor White
Write-Host "     python run.py" -ForegroundColor White
Write-Host ""
