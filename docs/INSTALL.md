# Установка и эксплуатация

## Быстрый старт

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/installer.sh)"
```

Для неинтерактивной установки:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/installer.sh)" -- --yes
```

## Параметры

```text
--yes
--ipv4 ADDRESS
--ipv6 ADDRESS
--hostname NAME
--no-ipv6
```

Пример с явным IP. Здесь используется документационный адрес `203.0.113.42`:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/installer.sh)" -- \
  --ipv4 203.0.113.42 \
  --hostname 203-0-113-42.nip.io
```

## Домены

После установки список selective routing находится в:

```text
/etc/smartdns/domains.txt
```

Применить изменения:

```bash
smartdns-rebuild
```

## Проверка

```bash
smartdns-health
```

## Логи

```bash
smartdns-logs
```

Или напрямую:

```bash
tail -F /var/log/nginx/smartdns-stream.log
```

## Сертификат

Let's Encrypt выпускается на автоматически созданный `nip.io` hostname. Certbot renewal hook после продления копирует сертификат для dnsdist и перезапускает `dnsdist` и `nginx`.
