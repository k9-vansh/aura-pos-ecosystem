# Aura POS - 100% Free Deployment Helper
param (
    [string]$RemoteUrl = ""
)

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Aura POS : 100% Free Deployment Automation" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Build all frontends into unified ecosystem
Write-Host "`n==> Step 1: Building all frontend applications..." -ForegroundColor Yellow
python scratch\assemble_ecosystem.py

# 2. Check Git Remote
if ($RemoteUrl -ne "") {
    Write-Host "`n==> Setting git remote origin to $RemoteUrl..." -ForegroundColor Yellow
    git remote remove origin -ErrorAction SilentlyContinue
    git remote add origin $RemoteUrl
}

$currentRemote = git remote get-url origin 2>$null
if (-not $currentRemote) {
    Write-Host "`n[!] No Git remote origin configured yet." -ForegroundColor Yellow
    Write-Host "Create a repo on GitHub (https://github.com/new), then run:" -ForegroundColor White
    Write-Host "  git remote add origin https://github.com/<YOUR_USERNAME>/aura-pos-ecosystem.git" -ForegroundColor Green
    Write-Host "  git push -u origin main" -ForegroundColor Green
    exit 0
}

Write-Host "`n==> Pushing latest code to GitHub ($currentRemote)..." -ForegroundColor Yellow
git add .
git commit -m "chore: update deployment bundles and configurations" -ErrorAction SilentlyContinue
git push -u origin main

Write-Host "`n[SUCCESS] Code pushed to GitHub!" -ForegroundColor Green
Write-Host "Render and Vercel will now automatically build and deploy your free instances." -ForegroundColor Cyan
