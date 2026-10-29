#!/usr/bin/env bash
# Install only the optional Telegram manager; do not rerun the DNS installer.
set -Eeuo pipefail
REF=main
CONFIGURE=0
while (($#)); do
  case "$1" in
    --ref) shift; REF=${1:?Missing ref} ;;
    --configure) CONFIGURE=1 ;;
    *) echo 'Usage: install-telegram-bot.sh [--ref REF] [--configure]' >&2; exit 1 ;;
  esac
  shift
done
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $REF =~ ^[a-zA-Z0-9_./-]+$ ]] || exit 1
for path in /etc/smartdns/domains.txt /etc/dnsdist/dnsdist.conf /etc/nginx/stream.d/smartdns.conf /usr/local/sbin/smartdns-rebuild; do
  [[ -f $path && ! -L $path ]] || { echo "Existing SmartDNS file required: $path" >&2; exit 1; }
done
command -v curl >/dev/null || { echo 'Install curl first.' >&2; exit 1; }
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
base="https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/$REF"
for file in bot.py control.py; do
  curl --fail --silent --show-error --location --max-time 45 "$base/telegram-bot/$file" -o "$tmp/$file"
done
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 sudo ca-certificates >/dev/null
python3 -m py_compile "$tmp/bot.py" "$tmp/control.py"
if ! id smartdns-bot >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/smartdns-bot --shell /usr/sbin/nologin smartdns-bot
fi
install -d -o root -g root -m 0755 /opt/smartdns-telegram-bot
install -d -o root -g root -m 0700 /etc/smartdns-telegram-bot /var/lib/smartdns-bot-backups
env_file=/etc/smartdns-telegram-bot/bot.env
if [[ ! -s $env_file || $CONFIGURE == 1 ]]; then
  read -r -s -p 'Telegram bot token (hidden): ' token </dev/tty
  printf '\n'
  read -r -p 'Administrator Telegram IDs, separated by commas: ' admins </dev/tty
  admins=${admins// /}
  [[ $token =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Invalid token format.' >&2; exit 1; }
  [[ $admins =~ ^[1-9][0-9]*(,[1-9][0-9]*)*$ ]] || { echo 'Use numeric Telegram IDs.' >&2; exit 1; }
  umask 077
  printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ADMIN_IDS=%s\n' "$token" "$admins" >"$tmp/bot.env"
  unset token
  install -o root -g root -m 0600 "$tmp/bot.env" "$env_file"
fi
systemctl stop smartdns-telegram-bot.service 2>/dev/null || true
install -o root -g root -m 0644 "$tmp/bot.py" "$tmp/control.py" /opt/smartdns-telegram-bot/
install -o root -g root -m 0755 "$tmp/control.py" /usr/local/sbin/smartdns-botctl
printf 'smartdns-bot ALL=(root) NOPASSWD: /usr/local/sbin/smartdns-botctl ""\n' >"$tmp/sudoers"
visudo -cf "$tmp/sudoers"
install -o root -g root -m 0440 "$tmp/sudoers" /etc/sudoers.d/smartdns-telegram-bot
cat >/etc/systemd/system/smartdns-telegram-bot.service <<'UNIT'
[Unit]
Description=SmartDNS private Telegram domain manager
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=smartdns-bot
Group=smartdns-bot
EnvironmentFile=/etc/smartdns-telegram-bot/bot.env
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory=/opt/smartdns-telegram-bot
ExecStart=/usr/bin/python3 -I /opt/smartdns-telegram-bot/bot.py
StateDirectory=smartdns-bot
StateDirectoryMode=0700
UMask=0077
Restart=on-failure
RestartSec=10
TimeoutStopSec=45
PrivateTmp=true
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
# The narrowly scoped sudo adapter must be allowed to acquire privileges.
# NoNewPrivileges/ProtectSystem=strict would also constrain that adapter.
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/logrotate.d/smartdns-telegram-bot <<'ROTATE'
/var/lib/smartdns-bot-backups/audit.jsonl {
    weekly
    rotate 8
    size 2M
    compress
    missingok
    notifempty
    create 0600 root root
}
ROTATE
systemctl daemon-reload
systemctl enable --now smartdns-telegram-bot.service
sleep 2
systemctl is-active --quiet smartdns-telegram-bot.service || {
  echo 'Bot did not start. Check: journalctl -u smartdns-telegram-bot -n 30 --no-pager' >&2
  exit 1
}
echo 'Installed. Open the bot in Telegram and send /start.'
echo 'Service: systemctl status smartdns-telegram-bot'
echo 'Logs: journalctl -u smartdns-telegram-bot -f'
