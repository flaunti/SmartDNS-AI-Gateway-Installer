#!/usr/bin/python3 -I
"""Root-owned, fixed-operation adapter for the existing SmartDNS installation."""
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

DOMAIN_FILE = Path('/etc/smartdns/domains.txt')
CONFIGS = (DOMAIN_FILE, Path('/etc/dnsdist/dnsdist.conf'), Path('/etc/nginx/stream.d/smartdns.conf'))
STATE = Path('/var/lib/smartdns-bot-backups')
REBUILD = '/usr/local/sbin/smartdns-rebuild'


def normalize(value):
    if not isinstance(value, str):
        raise ValueError('Домен должен быть строкой.')
    value = value.strip().rstrip('.').lower()
    if value.startswith('*.'):
        value = value[2:]
    if any(c in value for c in '/:@?#\\') or '.' not in value:
        raise ValueError('Нужен домен без протокола, порта и пути: example.com')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError('Укажите домен, а не IP-адрес.')
    value = value.encode('idna').decode('ascii')
    if len(value) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in value.split('.')):
        raise ValueError('Некорректный домен.')
    return value


def domains(raw):
    return sorted({normalize(s.split('#', 1)[0]) for s in raw.decode('utf-8-sig').splitlines() if s.split('#', 1)[0].strip()})


def version(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw, mode=0o600, uid=0, gid=0):
    fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), uid, gid)
        os.replace(name, path)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        Path(name).unlink(missing_ok=True)


def command(args, timeout=60):
    # Kill the entire rebuild group on timeout, before restoring live files.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C.UTF-8'}) as proc:
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise RuntimeError('Превышено время применения конфигурации.')
        if proc.returncode:
            raise RuntimeError(output.decode('utf-8', 'replace')[-2000:] or 'Команда завершилась ошибкой.')


class Controller:
    def __init__(self, files=CONFIGS, state=STATE, runner=command):
        self.files, self.state, self.runner = tuple(files), state, runner
        self.domain_file = self.files[0]

    def read(self):
        raw = self.domain_file.read_bytes()
        return {'domains':domains(raw), 'version':version(raw)}

    def snapshot(self):
        result = []
        for path in self.files:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Ожидался обычный файл: '+str(path))
            result.append({'data':base64.b64encode(path.read_bytes()).decode(),
                           'mode':stat.S_IMODE(info.st_mode), 'uid':info.st_uid, 'gid':info.st_gid})
        return result

    def restore(self, snapshot):
        if len(snapshot) != len(self.files):
            raise RuntimeError('Повреждена резервная копия.')
        for path, saved in zip(self.files, snapshot):
            atomic(path, base64.b64decode(saved['data'], validate=True), saved['mode'], saved['uid'], saved['gid'])
        self.runner(['/usr/sbin/dnsdist','--check-config','-C',str(self.files[1])])
        self.runner(['/usr/sbin/nginx','-t'])
        self.runner(['/usr/bin/systemctl','restart','dnsdist','nginx'])

    def recover(self):
        marker = self.state/'pending.json'
        if marker.exists():
            record = json.loads(marker.read_text())
            self.restore(record['snapshot'])
            self.discard_backup(record)
            marker.unlink()

    def discard_backup(self, record):
        name = record.get('backup','')
        if re.fullmatch(r'backup-[0-9]{20}-[a-f0-9]{8}\.json',name):
            (self.state/name).unlink(missing_ok=True)

    def audit(self, data):
        with (self.state/'audit.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(time=int(time.time()), **data), ensure_ascii=False)+'\n')

    def apply(self, request):
        current = self.domain_file.read_bytes()
        if request.get('version') != version(current):
            raise ValueError('Список уже изменился. Откройте его заново и повторите действие.')
        previous = domains(current)
        action = request['action']
        if action == 'undo':
            backups = sorted(self.state.glob('backup-*.json'))
            if not backups:
                raise ValueError('Резервных копий пока нет.')
            saved = json.loads(backups[-1].read_text())['snapshot'][0]
            updated = domains(base64.b64decode(saved['data']))
        else:
            values = request.get('domains')
            if not isinstance(values, list) or not 1 <= len(values) <= 50:
                raise ValueError('Передайте от 1 до 50 доменов.')
            selected = {normalize(x) for x in values}
            updated = sorted(set(previous) | selected if action == 'add' else set(previous) - selected)
        if not updated:
            raise ValueError('Нельзя удалить все домены.')
        if len(updated) > 5000:
            raise ValueError('Лимит — 5000 доменов.')
        if updated == previous:
            return {'changed':False, **self.read()}
        for service in ('dnsdist','nginx'):
            self.runner(['/usr/bin/systemctl','is-active','--quiet',service])
        snapshot = self.snapshot()
        name = 'backup-'+f'{time.time_ns():020d}'+'-'+uuid.uuid4().hex[:8]+'.json'
        record = {'snapshot':snapshot, 'actor':request['actor'], 'action':action,'backup':name}
        raw = json.dumps(record).encode()
        atomic(self.state/name, raw)
        atomic(self.state/'pending.json', raw)
        self.audit({'actor':request['actor'], 'action':action, 'result':'started'})
        try:
            meta = snapshot[0]
            atomic(self.domain_file, ('# Managed by SmartDNS Telegram Bot; suffixes include subdomains.\n'+'\n'.join(updated)+'\n').encode(),meta['mode'],meta['uid'],meta['gid'])
            self.runner([REBUILD], 90)
            for service in ('dnsdist','nginx'):
                self.runner(['/usr/bin/systemctl','is-active','--quiet',service])
        except Exception as exc:
            try:
                self.restore(snapshot)
            except Exception as rollback:
                self.audit({'actor':request['actor'],'action':action,'result':'rollback_failed'})
                raise RuntimeError('КРИТИЧНО: применение и откат не завершены. Нужна проверка сервера. '+str(rollback)) from exc
            self.discard_backup(record)
            (self.state/'pending.json').unlink()
            self.audit({'actor':request['actor'],'action':action,'result':'rolled_back'})
            raise RuntimeError('Изменение отменено, прежние файлы и сервисы восстановлены. '+str(exc)) from exc
        (self.state/'pending.json').unlink()
        self.audit({'actor':request['actor'],'action':action,'result':'applied','added':sorted(set(updated)-set(previous)),'removed':sorted(set(previous)-set(updated))})
        for old in sorted(self.state.glob('backup-*.json'))[:-20]:
            old.unlink()
        return {'changed':True, **self.read()}

    def handle(self, request):
        if request.get('action') not in ('list','status','add','remove','undo'):
            raise ValueError('Неизвестное действие.')
        if type(request.get('actor')) is not int or request['actor'] <= 0:
            raise ValueError('Не указан Telegram ID администратора.')
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state/'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.recover()
            if request['action'] == 'list':
                return self.read()
            if request['action'] == 'status':
                services = {}
                for name in ('unbound','dnsdist','nginx'):
                    try:
                        self.runner(['/usr/bin/systemctl','is-active','--quiet',name])
                        services[name] = True
                    except RuntimeError:
                        services[name] = False
                return {'services':services, **self.read()}
            return self.apply(request)


def main():
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Требуется запуск через разрешённый sudo helper.')
        request = json.loads(sys.stdin.buffer.read(65537))
        if not isinstance(request, dict):
            raise ValueError('Ожидался JSON-объект.')
        result = Controller().handle(request)
        print(json.dumps({'ok':True, **result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
