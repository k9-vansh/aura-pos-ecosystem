#!/usr/bin/env bash
# Exit on error
set -o errexit

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Collecting static files..."
python manage.py collectstatic --no-input

echo "==> Running database migrations..."
python manage.py migrate --no-input

echo "==> Seeding initial test data & pilot centers..."
python manage.py seed_test_team || true

echo "==> Seeding guaranteed demo credentials for 4 portals..."
python manage.py seed_demo_accounts

echo "==> Build completed successfully!"
