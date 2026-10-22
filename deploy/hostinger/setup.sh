#!/bin/bash
# Run on a Hostinger Ubuntu VPS as root:
#   DOMAIN=your-domain.com bash deploy/hostinger/setup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/pharmalink}"
DOMAIN="${DOMAIN:-}"
REPO_URL="${REPO_URL:-https://github.com/villanuevaangel-cyber/Pharmalink.git}"
BRANCH="${BRANCH:-cursor/render-deploy}"

if [ -z "$DOMAIN" ]; then
  echo "Set DOMAIN first, example:"
  echo "  DOMAIN=pharmalink.yourdomain.com bash deploy/hostinger/setup.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git nginx tesseract-ocr

mkdir -p "$(dirname "$APP_DIR")"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Fill /var/www/pharmalink/.env then run: systemctl restart pharmalink"
fi

sed -i "s/PUBLIC_BASE_URL=.*/PUBLIC_BASE_URL=https:\\/\\/${DOMAIN}/" .env || true
sed -i "s/PAYMONGO_BASE_URL=.*/PAYMONGO_BASE_URL=https:\\/\\/${DOMAIN}/" .env || true

chown -R www-data:www-data "$APP_DIR"

cp deploy/hostinger/pharmalink.service /etc/systemd/system/pharmalink.service
sed "s/DOMAIN_HERE/${DOMAIN}/g" deploy/hostinger/nginx.conf > /etc/nginx/sites-available/pharmalink
ln -sfn /etc/nginx/sites-available/pharmalink /etc/nginx/sites-enabled/pharmalink
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable --now pharmalink
systemctl reload nginx

echo
echo "Next:"
echo "  1. Point the domain A record to this VPS IP."
echo "  2. Put real SMTP and Supabase values in $APP_DIR/.env"
echo "  3. systemctl restart pharmalink"
echo "  4. apt-get install -y certbot python3-certbot-nginx"
echo "  5. certbot --nginx -d $DOMAIN"
