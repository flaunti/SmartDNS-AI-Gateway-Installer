#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="1.3.0"
PROJECT_NAME="SmartDNS AI Gateway"
STATE_DIR="/etc/smartdns"
DOMAINS_FILE="$STATE_DIR/domains.txt"
STATE_FILE="$STATE_DIR/config.env"
DNSDIST_TLS_DIR="/etc/dnsdist/tls"
UNBOUND_CONF="/etc/unbound/unbound.conf.d/smartdns.conf"
DNSDIST_CONF="/etc/dnsdist/dnsdist.conf"
NGINX_STREAM_DIR="/etc/nginx/stream.d"
NGINX_STREAM_CONF="$NGINX_STREAM_DIR/smartdns.conf"
IOS_PROFILE="/root/SmartDNS-iOS.mobileconfig"
YES=0
IPV4=""
IPV6=""
HOSTNAME=""

log()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
$PROJECT_NAME installer v$VERSION

Usage:
  bash install-smartdns.sh [options]

Options:
  --yes                 Run without confirmation.
  --ipv4 ADDRESS        Override detected public IPv4.
  --ipv6 ADDRESS        Override detected public IPv6.
  --hostname NAME       Override generated nip.io hostname.
  --no-ipv6             Disable IPv6 publishing even if detected.
  --help                Show this help.

Environment:
  SMARTDNS_SKIP_UFW=1           Do not manage UFW.
  SMARTDNS_ALLOW_UNSUPPORTED=1 Allow Ubuntu versions other than 24.04.
USAGE
}

while (($#)); do
  case "$1" in
    --yes) YES=1 ;;
    --ipv4) shift; IPV4="${1:-}" ;;
    --ipv6) shift; IPV6="${1:-}" ;;
    --hostname) shift; HOSTNAME="${1:-}" ;;
    --no-ipv6) IPV6="disabled" ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || fail "Run the installer as root."
command -v curl >/dev/null 2>&1 || fail "curl is required to start the installer."

source /etc/os-release
if [[ ${ID:-} != "ubuntu" ]]; then
  fail "Supported OS: Ubuntu 24.04 LTS. Detected: ${PRETTY_NAME:-unknown}."
fi
if [[ ${VERSION_ID:-} != "24.04" && ${SMARTDNS_ALLOW_UNSUPPORTED:-0} != "1" ]]; then
  fail "Supported OS: Ubuntu 24.04 LTS. Set SMARTDNS_ALLOW_UNSUPPORTED=1 to override."
fi

valid_ipv4() {
  local ip=$1 IFS=. octets
  read -r -a octets <<< "$ip"
  [[ ${#octets[@]} -eq 4 ]] || return 1
  local o
  for o in "${octets[@]}"; do
    [[ $o =~ ^[0-9]+$ ]] || return 1
    ((10#$o >= 0 && 10#$o <= 255)) || return 1
  done
}

if [[ -z $IPV4 ]]; then
  IPV4="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
fi
valid_ipv4 "$IPV4" || fail "Could not determine a valid public IPv4. Use --ipv4 ADDRESS."

if [[ $IPV6 != "disabled" && -z $IPV6 ]]; then
  IPV6="$(curl -6fsS --max-time 8 https://api64.ipify.org 2>/dev/null || true)"
fi
if [[ $IPV6 == "disabled" ]]; then
  IPV6=""
elif [[ -n $IPV6 && $IPV6 != *:* ]]; then
  warn "Detected IPv6 looks invalid; IPv6 publishing will be disabled."
  IPV6=""
fi

if [[ -z $HOSTNAME ]]; then
  HOSTNAME="${IPV4//./-}.nip.io"
fi

cat <<SUMMARY

$PROJECT_NAME v$VERSION

IPv4:      $IPV4
IPv6:      ${IPV6:-disabled}
Hostname:  $HOSTNAME
DoH:       https://$HOSTNAME/dns-query
DoT:       $HOSTNAME:853

The installer will configure Unbound, dnsdist, nginx stream, Let's Encrypt,
selective AI routing, health checks and an iOS DNS profile.
SUMMARY

if ((YES == 0)); then
  read -r -p "Continue? [Y/n] " answer
  case "${answer:-Y}" in
    y|Y|yes|YES) ;;
    *) exit 0 ;;
  esac
fi

export DEBIAN_FRONTEND=noninteractive
log "Installing packages..."
apt-get update -qq
apt-get install -y -qq \
  unbound dnsdist nginx libnginx-mod-stream certbot dnsutils \
  curl ca-certificates openssl python3 ufw >/dev/null
ok "Packages installed"

systemctl stop nginx dnsdist unbound 2>/dev/null || true

mkdir -p "$STATE_DIR" "$DNSDIST_TLS_DIR" "$NGINX_STREAM_DIR"
chmod 755 "$STATE_DIR"

cat > "$DOMAINS_FILE" <<'DOMAINS'
chatgpt.com
openai.com
oaistatic.com
oaiusercontent.com
claude.ai
anthropic.com
claudeusercontent.com
krea.ai
civitai.com
gemini.google.com
aistudio.google.com
generativelanguage.googleapis.com
robinfrontend-pa.googleapis.com
DOMAINS

cat > "$STATE_FILE" <<STATE
IPV4='$IPV4'
IPV6='$IPV6'
HOSTNAME='$HOSTNAME'
VERSION='$VERSION'
STATE
chmod 600 "$STATE_FILE"

cat > "$UNBOUND_CONF" <<'UNBOUND'
server:
    interface: 127.0.0.1
    port: 5353
    access-control: 127.0.0.0/8 allow
    access-control: ::1/128 allow
    do-ip4: yes
    do-ip6: yes
    prefer-ip6: no
    hide-identity: yes
    hide-version: yes
    qname-minimisation: yes
    harden-glue: yes
    harden-dnssec-stripped: yes
    prefetch: yes
    rrset-roundrobin: yes
UNBOUND

systemctl enable unbound >/dev/null 2>&1 || true
systemctl restart unbound
for _ in {1..30}; do
  if dig @127.0.0.1 -p 5353 github.com A +short +time=1 +tries=1 | grep -q .; then
    break
  fi
  sleep 1
done
dig @127.0.0.1 -p 5353 github.com A +short +time=2 +tries=1 | grep -q . \
  || fail "Unbound did not start correctly on 127.0.0.1:5353."
ok "Unbound origin resolver is ready"

if [[ ${SMARTDNS_SKIP_UFW:-0} != "1" ]]; then
  log "Configuring UFW..."
  mapfile -t SSH_PORTS < <(sshd -T 2>/dev/null | awk '/^port /{print $2}' | sort -un || true)
  if ((${#SSH_PORTS[@]} == 0)); then SSH_PORTS=(22); fi
  for port in "${SSH_PORTS[@]}"; do ufw allow "$port/tcp" >/dev/null; done
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 853/tcp >/dev/null
  ufw --force enable >/dev/null
  ok "UFW enabled; UDP/53 and UDP/443 remain closed"
else
  warn "UFW management skipped by SMARTDNS_SKIP_UFW=1"
fi

log "Checking nip.io hostname..."
resolved_v4="$(getent ahostsv4 "$HOSTNAME" | awk 'NR==1{print $1}' || true)"
if [[ $resolved_v4 != "$IPV4" ]]; then
  warn "$HOSTNAME currently resolves to '${resolved_v4:-nothing}', expected $IPV4. Waiting up to 60 seconds..."
  for _ in {1..12}; do
    sleep 5
    resolved_v4="$(getent ahostsv4 "$HOSTNAME" | awk 'NR==1{print $1}' || true)"
    [[ $resolved_v4 == "$IPV4" ]] && break
  done
fi
[[ $resolved_v4 == "$IPV4" ]] || fail "$HOSTNAME does not resolve to $IPV4."
ok "$HOSTNAME resolves to $IPV4"

log "Requesting Let's Encrypt certificate..."
systemctl stop nginx 2>/dev/null || true
certbot certonly --standalone \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --preferred-challenges http -d "$HOSTNAME"
ok "Certificate issued for $HOSTNAME"

install -o _dnsdist -g _dnsdist -m 0640 "/etc/letsencrypt/live/$HOSTNAME/fullchain.pem" "$DNSDIST_TLS_DIR/fullchain.pem"
install -o _dnsdist -g _dnsdist -m 0640 "/etc/letsencrypt/live/$HOSTNAME/privkey.pem" "$DNSDIST_TLS_DIR/privkey.pem"

cat > /usr/local/sbin/smartdns-rebuild <<'REBUILD'
#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/smartdns/config.env
DOMAINS_FILE=/etc/smartdns/domains.txt
DNSDIST_CONF=/etc/dnsdist/dnsdist.conf
NGINX_STREAM_CONF=/etc/nginx/stream.d/smartdns.conf
CERT=/etc/dnsdist/tls/fullchain.pem
KEY=/etc/dnsdist/tls/privkey.pem

mapfile -t DOMAINS < <(sed -E 's/[[:space:]]*#.*$//; s/^[[:space:]]+|[[:space:]]+$//g' "$DOMAINS_FILE" | awk 'NF' | sort -u)
((${#DOMAINS[@]})) || { echo "domains.txt is empty" >&2; exit 1; }

{
  cat <<EOF
setLocal("127.0.0.1:5300")
setACL({"0.0.0.0/0", "::/0"})
newServer({address="127.0.0.1:5353", name="unbound"})
setServerPolicy(firstAvailable)

addDOHLocal("127.0.0.1:4443", "$CERT", "$KEY", "/dns-query")
addTLSLocal("0.0.0.0:853", "$CERT", "$KEY")
EOF
  if [[ -n ${IPV6:-} ]]; then
    echo "addTLSLocal(\"[::]:853\", \"$CERT\", \"$KEY\")"
  fi
  cat <<'EOF'

aiDomains = newSuffixMatchNode()
aiDomains:add({
EOF
  for d in "${DOMAINS[@]}"; do printf '  "%s",\n' "$d"; done
  cat <<EOF
})

addAction(AndRule({SuffixMatchNodeRule(aiDomains), QTypeRule(DNSQType.A)}), SpoofAction("$IPV4"))
EOF
  if [[ -n ${IPV6:-} ]]; then
    printf 'addAction(AndRule({SuffixMatchNodeRule(aiDomains), QTypeRule(DNSQType.AAAA)}), SpoofAction("%s"))\n' "$IPV6"
  else
    cat <<'EOF'
addAction(AndRule({SuffixMatchNodeRule(aiDomains), QTypeRule(DNSQType.AAAA)}), RCodeAction(DNSRCode.NOERROR))
EOF
  fi
  cat <<'EOF'
addAction(AndRule({SuffixMatchNodeRule(aiDomains), QTypeRule(64)}), RCodeAction(DNSRCode.NOERROR))
addAction(AndRule({SuffixMatchNodeRule(aiDomains), QTypeRule(65)}), RCodeAction(DNSRCode.NOERROR))
EOF
} > "$DNSDIST_CONF.tmp"

install -o root -g _dnsdist -m 0640 "$DNSDIST_CONF.tmp" "$DNSDIST_CONF"
rm -f "$DNSDIST_CONF.tmp"

{
  cat <<EOF
map \$ssl_preread_server_name \$smartdns_backend {
    hostnames;
    ""                         127.0.0.1:4443;
    "$HOSTNAME"               127.0.0.1:4443;
EOF
  for d in "${DOMAINS[@]}"; do
    esc="${d//./\\.}"
    printf '    ~^(?:.*\\.)?%s$    $ssl_preread_server_name:443;\n' "$esc"
  done
  cat <<'EOF'
    default                    127.0.0.1:9;
}

log_format smartdns_stream '$remote_addr [$time_local] sni="$ssl_preread_server_name" upstream="$upstream_addr" status=$status bytes_sent=$bytes_sent bytes_received=$bytes_received session=$session_time';

server {
    listen 443;
EOF
  if [[ -n ${IPV6:-} ]]; then
    echo '    listen [::]:443;'
  fi
  cat <<'EOF'
    ssl_preread on;
    resolver 127.0.0.1:5353 ipv6=off valid=300s;
    proxy_connect_timeout 10s;
    proxy_timeout 300s;
    proxy_pass $smartdns_backend;
    access_log /var/log/nginx/smartdns-stream.log smartdns_stream;
}
EOF
} > "$NGINX_STREAM_CONF"

dnsdist --check-config -C "$DNSDIST_CONF"
nginx -t
systemctl restart dnsdist
systemctl restart nginx

echo "SmartDNS configuration rebuilt successfully."
REBUILD
chmod 755 /usr/local/sbin/smartdns-rebuild

python3 - <<'PY'
from pathlib import Path
import re

p = Path('/etc/nginx/nginx.conf')
s = p.read_text()
marker = 'include /etc/nginx/stream.d/*.conf;'

if marker not in s:
    match = re.search(r'(?m)^\s*stream\s*\{', s)
    if match:
        depth = 0
        end = None
        for i in range(match.end() - 1, len(s)):
            if s[i] == '{':
                depth += 1
            elif s[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            raise SystemExit('Existing nginx stream block is malformed')
        s = s[:end] + '    include /etc/nginx/stream.d/*.conf;\n' + s[end:]
    else:
        s = s.rstrip() + '\n\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n'

p.write_text(s)
PY

smartdns-rebuild
systemctl enable dnsdist nginx >/dev/null 2>&1 || true
ok "dnsdist and nginx are configured"

cat > /etc/letsencrypt/renewal-hooks/deploy/smartdns-reload.sh <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
install -o _dnsdist -g _dnsdist -m 0640 "/etc/letsencrypt/live/$HOSTNAME/fullchain.pem" "$DNSDIST_TLS_DIR/fullchain.pem"
install -o _dnsdist -g _dnsdist -m 0640 "/etc/letsencrypt/live/$HOSTNAME/privkey.pem" "$DNSDIST_TLS_DIR/privkey.pem"
systemctl restart dnsdist nginx
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/smartdns-reload.sh

cat > /usr/local/sbin/smartdns-health <<'HEALTH'
#!/usr/bin/env bash
set -u
source /etc/smartdns/config.env
PASS=0
FAIL=0
check() {
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then
    printf '[ OK ] %s\n' "$name"; PASS=$((PASS+1))
  else
    printf '[FAIL] %s\n' "$name"; FAIL=$((FAIL+1))
  fi
}
check "Unbound service" systemctl is-active --quiet unbound
check "dnsdist service" systemctl is-active --quiet dnsdist
check "nginx service" systemctl is-active --quiet nginx
check "Origin resolver" bash -c 'dig @127.0.0.1 -p 5353 github.com A +short +time=2 +tries=1 | grep -q .'
check "SmartDNS spoof" bash -c "dig @127.0.0.1 -p 5300 chatgpt.com A +short +time=2 +tries=1 | grep -qx '$IPV4'"
check "DoH TLS/certificate" bash -c "code=\$(curl -sS --resolve '$HOSTNAME:443:$IPV4' -o /dev/null -w '%{http_code}' 'https://$HOSTNAME/dns-query'); [[ \$code == 400 || \$code == 405 ]]"
check "DoT TLS" bash -c "echo | openssl s_client -connect '$IPV4:853' -servername '$HOSTNAME' -verify_hostname '$HOSTNAME' 2>/dev/null | grep -q 'Verify return code: 0'"
check "AI TLS gateway" bash -c "echo | openssl s_client -connect '$IPV4:443' -servername chatgpt.com 2>/dev/null | grep -qi 'subject=.*chatgpt.com'"
printf '\nPASS=%d FAIL=%d\n' "$PASS" "$FAIL"
((FAIL == 0))
HEALTH
chmod 755 /usr/local/sbin/smartdns-health

cat > /usr/local/sbin/smartdns-logs <<'LOGS'
#!/usr/bin/env bash
exec tail -F /var/log/nginx/smartdns-stream.log
LOGS
chmod 755 /usr/local/sbin/smartdns-logs

PROFILE_UUID="$(cat /proc/sys/kernel/random/uuid | tr '[:lower:]' '[:upper:]')"
DNS_UUID="$(cat /proc/sys/kernel/random/uuid | tr '[:lower:]' '[:upper:]')"
cat > "$IOS_PROFILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>DNSSettings</key>
      <dict>
        <key>DNSProtocol</key><string>HTTPS</string>
        <key>ServerURL</key><string>https://$HOSTNAME/dns-query</string>
      </dict>
      <key>PayloadDescription</key><string>SmartDNS AI Gateway DNS-over-HTTPS</string>
      <key>PayloadDisplayName</key><string>SmartDNS AI Gateway</string>
      <key>PayloadIdentifier</key><string>com.smartdns.ai.dns</string>
      <key>PayloadType</key><string>com.apple.dnsSettings.managed</string>
      <key>PayloadUUID</key><string>$DNS_UUID</string>
      <key>PayloadVersion</key><integer>1</integer>
    </dict>
  </array>
  <key>PayloadDescription</key><string>SmartDNS AI Gateway profile</string>
  <key>PayloadDisplayName</key><string>SmartDNS AI Gateway</string>
  <key>PayloadIdentifier</key><string>com.smartdns.ai.profile</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>$PROFILE_UUID</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
EOF
chmod 600 "$IOS_PROFILE"

log "Running health check..."
if smartdns-health; then
  ok "Installation completed successfully"
else
  warn "Installation completed, but one or more health checks failed. Run: smartdns-health"
fi

cat <<DONE

========================================
$PROJECT_NAME is installed
========================================

Browser DoH:
  https://$HOSTNAME/dns-query

Android Private DNS:
  $HOSTNAME

iPhone/iPad profile:
  $IOS_PROFILE

Domains:
  $DOMAINS_FILE

Apply domain changes:
  smartdns-rebuild

Health check:
  smartdns-health

Gateway logs:
  smartdns-logs

Public DNS ports:
  TCP 443  DoH + selective TLS/SNI gateway
  TCP 853  DoT

UDP/TCP 53 are not exposed publicly.
DONE
