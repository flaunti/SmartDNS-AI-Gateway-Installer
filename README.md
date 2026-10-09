# SmartDNS AI Gateway

Selective Smart DNS gateway для AI-сервисов без покупки собственного домена.

Проект автоматически использует `nip.io`, поднимает DNS-over-HTTPS и DNS-over-TLS, а трафик только выбранных доменов отправляет через VPS. Остальные сайты продолжают открываться напрямую.

## Установка

На чистом Ubuntu 24.04 LTS достаточно выполнить:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/installer.sh)"
```

Без подтверждения:

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/flaunti/SmartDNS-AI-Gateway-Installer/refs/heads/main/installer.sh)" -- --yes
```

Полная логика установки находится в [`install-smartdns.sh`](install-smartdns.sh). Его можно проверить перед запуском.

## Что умеет

- DoH для браузеров: `https://<IP-с-дефисами>.nip.io/dns-query`
- DoT для Android Private DNS: `<IP-с-дефисами>.nip.io`
- готовый `.mobileconfig` для iOS/iPadOS;
- selective routing только для выбранных доменов;
- TLS passthrough без MITM и установки собственного CA;
- IPv4 и IPv6;
- автоматический Let's Encrypt сертификат и renewal;
- healthcheck, live-логи и простой список доменов;
- UFW-конфигурация без публичного UDP/53;
- блокировка HTTPS/SVCB для проксируемых доменов, чтобы браузер не обходил gateway через origin hints/ECH.

## Поддерживаемые сервисы по умолчанию

- ChatGPT / OpenAI
- Claude / Anthropic
- Krea
- Civitai
- Gemini Web / Google AI Studio
- встроенный Gemini на Android/Samsung (`robinfrontend-pa.googleapis.com`)

`*.google.com` целиком намеренно не проксируется.

После установки список доменов находится в:

```text
/etc/smartdns/domains.txt
```

После изменения списка:

```bash
smartdns-rebuild
```

## Требования

- чистый Ubuntu 24.04 LTS VPS;
- публичный IPv4;
- IPv6 желательно, но необязательно;
- свободные TCP-порты `80`, `443`, `853`;
- root-доступ;
- нужные AI-сервисы должны быть доступны с IP VPS.

## Как это работает

```text
Browser / Android / iOS
          │
     DoH / DoT
          │
          ▼
       dnsdist
          │
          ├─ обычный домен ───────────────► Unbound ─► настоящий IP
          │
          └─ AI-домен ─► IP VPS
                         │
                         ▼
                    nginx stream
                         │
                    SNI passthrough
                         │
                         └───────────────► настоящий AI origin:443
```

Для AI HTTPS nginx читает только SNI из TLS ClientHello и делает TCP passthrough. TLS не расшифровывается, сертификат целевого сайта остаётся настоящим.

## Порты

Публично используются:

```text
80/tcp   Let's Encrypt HTTP-01
443/tcp  DoH + selective AI TLS gateway
853/tcp  DoT / Android Private DNS
```

`UDP/53`, `TCP/53` и `UDP/443` не открываются.

Внутренние listener'ы:

```text
127.0.0.1:5353  Unbound origin resolver
127.0.0.1:5300  dnsdist diagnostic DNS
127.0.0.1:4443  dnsdist DoH behind nginx stream
```

## Настройка клиентов

Ниже используется документационный адрес `203.0.113.42`.

### Chrome / Edge / Firefox

DoH URL:

```text
https://203-0-113-42.nip.io/dns-query
```

### Android Private DNS

В `Private DNS provider hostname` указывается только hostname:

```text
203-0-113-42.nip.io
```

Android использует DNS-over-TLS на TCP/853.

### iPhone / iPad

Установщик создаёт профиль:

```text
/root/SmartDNS-iOS.mobileconfig
```

Внутри профиля уже указан DoH endpoint текущего VPS.

## Диагностика

Общая проверка:

```bash
smartdns-health
```

Лог TLS/SNI gateway:

```bash
smartdns-logs
```

Статусы сервисов:

```bash
systemctl status unbound dnsdist nginx
```

Проверить selective DNS вручную:

```bash
dig @127.0.0.1 -p 5300 chatgpt.com A +short
dig @127.0.0.1 -p 5353 chatgpt.com A +short
```

Первый запрос должен вернуть IP VPS, второй — настоящий IP origin.

## Что установщик меняет на сервере

Основные файлы:

```text
/etc/smartdns/config.env
/etc/smartdns/domains.txt
/etc/unbound/unbound.conf.d/smartdns.conf
/etc/dnsdist/dnsdist.conf
/etc/dnsdist/tls/fullchain.pem
/etc/dnsdist/tls/privkey.pem
/etc/nginx/stream.d/smartdns.conf
/etc/letsencrypt/renewal-hooks/deploy/smartdns-reload.sh
/usr/local/sbin/smartdns-rebuild
/usr/local/sbin/smartdns-health
/usr/local/sbin/smartdns-logs
/root/SmartDNS-iOS.mobileconfig
```

Устанавливаемые пакеты: `unbound`, `dnsdist`, `nginx`, stream-модуль nginx, `certbot`, `dnsutils`, `openssl`, `python3` и `ufw`.

## Безопасность

- обычный DNS на публичном `53/tcp` и `53/udp` не поднимается;
- Unbound доступен только через loopback;
- DNS-трафик клиентов идёт через DoH или DoT;
- AI HTTPS не расшифровывается;
- неизвестный SNI на `443/tcp` не проксируется наружу;
- конфигурация проверяется `smartdns-health`.

Подробности: [`docs/INSTALL.md`](docs/INSTALL.md).

## Версия

Текущая версия: **1.3.0**.
