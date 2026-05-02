#!/usr/bin/env bash
# ============================================================
# Validate Railway Deployment Setup
# ============================================================
# Run this before deploying to Railway to check that all
# required configuration files are present and correct.

set -e

echo "=========================================="
echo "Railway Deployment Setup Validator"
echo "=========================================="
echo ""

ERRORS=0
WARNINGS=0

# Color codes
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

error() {
    echo -e "${RED}✗ ERROR: $1${NC}"
    ERRORS=$((ERRORS + 1))
}

warning() {
    echo -e "${YELLOW}⚠ WARNING: $1${NC}"
    WARNINGS=$((WARNINGS + 1))
}

success() {
    echo -e "${GREEN}✓ $1${NC}"
}

# Check required files
echo "Checking required files..."
echo ""

if [ ! -f "railway.toml" ]; then
    error "railway.toml not found"
else
    success "railway.toml exists"
fi

if [ ! -f "Procfile" ]; then
    error "Procfile not found"
else
    success "Procfile exists"
    # Check Procfile content
    if grep -q "python3 -m app.main" Procfile; then
        success "Procfile has correct start command"
    else
        error "Procfile missing 'python3 -m app.main' command"
    fi
fi

if [ ! -f ".railwayignore" ]; then
    error ".railwayignore not found"
else
    success ".railwayignore exists"
    # Check that .env is ignored
    if grep -q "^\.env" .railwayignore; then
        success ".railwayignore excludes .env (good!)"
    else
        error ".railwayignore does NOT exclude .env (security risk!)"
    fi
fi

if [ ! -f "nixpacks.toml" ]; then
    warning "nixpacks.toml not found (Railway will use defaults)"
else
    success "nixpacks.toml exists"
fi

if [ ! -f "requirements.txt" ]; then
    error "requirements.txt not found"
else
    success "requirements.txt exists"
fi

echo ""
echo "Checking Python dependencies..."
echo ""

# Check that required packages are in requirements.txt
required_packages=("nicegui" "httpx" "hyperliquid-python-sdk" "websockets")
for pkg in "${required_packages[@]}"; do
    if grep -q "$pkg" requirements.txt; then
        success "$pkg in requirements.txt"
    else
        error "$pkg missing from requirements.txt"
    fi
done

echo ""
echo "Checking .env file..."
echo ""

if [ -f ".env" ]; then
    warning ".env file exists locally (good for development)"
    echo "  Remember: .env is NOT deployed to Railway (it's in .railwayignore)"
    echo "  You must set environment variables in Railway dashboard"
else
    success ".env not present (will use Railway env vars)"
fi

if [ -f "env.example" ]; then
    success "env.example exists (reference for Railway env vars)"
else
    warning "env.example not found (users won't know what env vars to set)"
fi

echo ""
echo "Checking .gitignore..."
echo ""

if [ ! -f ".gitignore" ]; then
    warning ".gitignore not found"
else
    if grep -q "^\.env" .gitignore; then
        success ".gitignore excludes .env (prevents accidental commits)"
    else
        error ".gitignore does NOT exclude .env (SECURITY RISK!)"
    fi
fi

echo ""
echo "Checking project structure..."
echo ""

if [ ! -d "app" ]; then
    error "app/ directory not found"
else
    success "app/ directory exists"
fi

if [ ! -f "app/main.py" ]; then
    error "app/main.py not found (entry point)"
else
    success "app/main.py exists"
fi

required_modules=("portal.py" "hyperliquid_client.py" "db.py" "notifier.py")
for mod in "${required_modules[@]}"; do
    if [ -f "app/$mod" ]; then
        success "app/$mod exists"
    else
        error "app/$mod missing"
    fi
done

if [ ! -d "scripts" ]; then
    warning "scripts/ directory not found (not required for Railway)"
else
    success "scripts/ directory exists"
fi

echo ""
echo "=========================================="
echo "Validation Summary"
echo "=========================================="

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed!${NC}"
    echo ""
    echo "Your project is ready for Railway deployment."
    echo ""
    echo "Next steps:"
    echo "  1. Commit and push to GitHub"
    echo "  2. Create a Railway project from your repo"
    echo "  3. Add a volume (mount to /data)"
    echo "  4. Set environment variables in Railway dashboard"
    echo "  5. See RAILWAY_DEPLOYMENT.md for detailed instructions"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}Validation completed with $WARNINGS warning(s)${NC}"
    echo ""
    echo "You can deploy, but review the warnings above."
    echo "See RAILWAY_DEPLOYMENT.md for guidance."
elif [ $ERRORS -eq 1 ]; then
    echo -e "${RED}Validation failed with 1 error and $WARNINGS warning(s)${NC}"
    echo ""
    echo "Fix the error above before deploying."
    exit 1
else
    echo -e "${RED}Validation failed with $ERRORS errors and $WARNINGS warning(s)${NC}"
    echo ""
    echo "Fix the errors above before deploying."
    exit 1
fi

echo ""
