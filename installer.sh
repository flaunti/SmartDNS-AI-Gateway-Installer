#!/bin/sh
set -eu

SOURCE_URL="https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/install-smartdns.sh"
TMP_FILE="$(mktemp /tmp/smartdns-installer.XXXXXX.sh)"

cleanup() {
  rm -f "$TMP_FILE"
}
trap cleanup EXIT HUP INT TERM

if ! command -v curl >/dev/null 2>&1; then
  echo '[FAIL] curl is required.' >&2
  exit 1
fi

curl -fsSL "$SOURCE_URL" -o "$TMP_FILE"
chmod 700 "$TMP_FILE"
bash "$TMP_FILE" "$@"
