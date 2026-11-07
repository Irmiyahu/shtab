#!/usr/bin/env bash
# Установка штаба на чистый сервер Ubuntu 22.04 / 24.04.
# Запуск:  curl -fsSL https://raw.githubusercontent.com/ИМЯ/shtab/main/install.sh -o install.sh && sudo bash install.sh ИМЯ
# ИМЯ — имя аккаунта GitHub, где лежат файлы штаба. Повторный запуск обновляет код и сохраняет данные и настройки.
set -euo pipefail

GH_USER="${1:-}"
REPO="${2:-shtab}"
if [ -z "$GH_USER" ]; then echo "Укажите имя аккаунта GitHub: sudo bash install.sh ИМЯ"; exit 1; fi
if [ "$(id -u)" != "0" ]; then echo "Запустите через sudo"; exit 1; fi
RAW="https://raw.githubusercontent.com/$GH_USER/$REPO/main"
APP=/opt/shtab

echo "== 1/6 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip ffmpeg curl unzip ca-certificates >/dev/null

echo "== 2/6 Файлы штаба"
id shtab >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin shtab
mkdir -p "$APP/app" "$APP/web" "$APP/data"
curl -fsSL "$RAW/main.py" -o "$APP/app/main.py"
curl -fsSL "$RAW/index.html" -o "$APP/web/index.html"
curl -fsSL "$RAW/requirements.txt" -o "$APP/requirements.txt"
touch "$APP/app/__init__.py"

echo "== 3/6 Настройки"
if [ ! -f "$APP/.env" ]; then
  read -rp "Токен бота от @BotFather: " BOT_TOKEN </dev/tty
  read -rp "Telegram ID владельца (число от @userinfobot): " OWNER_ID </dev/tty
  read -rp "Ключ авторизации GigaChat: " GIGA_KEY </dev/tty
  IP="$(curl -fsS4 --max-time 10 https://ifconfig.me || true)"
  if [ -z "$IP" ]; then read -rp "Публичный IP сервера: " IP </dev/tty; fi
  DOMAIN="${IP//./-}.sslip.io"
  cat > "$APP/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
OWNER_ID=$OWNER_ID
GIGA_KEY=$GIGA_KEY
GIGA_MODEL=GigaChat-2-Pro
PUBLIC_URL=https://$DOMAIN
DOMAIN=$DOMAIN
EOF
  chmod 600 "$APP/.env"
fi
DOMAIN="$(grep '^DOMAIN=' "$APP/.env" | cut -d= -f2)"

echo "== 4/6 Python, распознавание речи, сертификат Минцифры для GigaChat"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
if [ ! -d "$APP/vosk-model" ]; then
  curl -fsSL https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip -o /tmp/vosk.zip
  unzip -q /tmp/vosk.zip -d /tmp/vosk && mv /tmp/vosk/vosk-model-small-ru-0.22 "$APP/vosk-model" && rm -rf /tmp/vosk /tmp/vosk.zip
fi
curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o "$APP/russian_trusted_root_ca.pem" \
  || echo "   (сертификат не скачался — штаб будет работать без проверки сертификата GigaChat)"
chown -R shtab:shtab "$APP"

echo "== 5/6 HTTPS (Caddy) для адреса https://$DOMAIN"
if [ ! -x /usr/local/bin/caddy ]; then
  curl -fsSL https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz -o /tmp/caddy.tgz
  tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy && rm /tmp/caddy.tgz
fi
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
  encode gzip
  reverse_proxy 127.0.0.1:8000
}
EOF
cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy HTTPS
After=network-online.target
[Service]
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
Restart=always
Environment=HOME=/root
[Install]
WantedBy=multi-user.target
EOF

echo "== 6/6 Автозапуск штаба"
cat > /etc/systemd/system/shtab.service <<EOF
[Unit]
Description=Shtab
After=network-online.target
[Service]
User=shtab
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q caddy shtab
systemctl restart caddy shtab

sleep 3
if systemctl is-active -q shtab; then
  echo
  echo "Готово. Штаб: https://$DOMAIN"
  echo "Откройте своего бота в Telegram и отправьте /start."
else
  echo "Штаб не запустился. Покажите последние строки журнала:"
  journalctl -u shtab -n 30 --no-pager
fi
