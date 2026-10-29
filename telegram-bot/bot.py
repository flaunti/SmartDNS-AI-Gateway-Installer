#!/usr/bin/python3 -I
"""Private Telegram UI for SmartDNS. Python 3.10+, standard library only."""
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

LOG = logging.getLogger('smartdns-bot')
MENU = {'inline_keyboard':[
    [{'text':'Домены','callback_data':'list:0'},{'text':'Поиск','callback_data':'search'}],
    [{'text':'Добавить','callback_data':'add'},{'text':'Удалить','callback_data':'remove'}],
    [{'text':'Сервисы','callback_data':'status'},{'text':'Откатить изменение','callback_data':'undo'}]]}


class ApiError(RuntimeError):
    def __init__(self, code, retry=5):
        self.code, self.retry = code, retry
        super().__init__('Telegram API: '+str(code))


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot'+token+'/'

    def call(self, method, **data):
        request = urllib.request.Request(self.base+method, json.dumps(data).encode(), {'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise ApiError(exc.code) from None
        except (OSError, ValueError):
            # Never include request URLs in exceptions: URLs contain the bot token.
            raise ApiError(0) from None
        if not result.get('ok'):
            raise ApiError(result.get('error_code',0), min(60, max(1,result.get('parameters',{}).get('retry_after',5))))
        return result['result']


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS intents (nonce TEXT PRIMARY KEY, uid INTEGER, chat INTEGER, expires REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS prompts (uid INTEGER PRIMARY KEY, mode TEXT, expires REAL);
        ''')

    def offset(self, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('offset',str(value)))
        row = self.db.execute("SELECT value FROM kv WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def prompt(self, uid, mode=None):
        if mode is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO prompts VALUES (?,?,?)',(uid,mode,time.time()+600))
            return mode
        row = self.db.execute('SELECT mode,expires FROM prompts WHERE uid=?',(uid,)).fetchone()
        return row[0] if row and row[1]>time.time() else ''

    def intent(self, uid, chat, payload):
        nonce = secrets.token_hex(12)
        with self.db:
            self.db.execute('DELETE FROM intents WHERE expires<?',(time.time(),))
            self.db.execute('INSERT INTO intents VALUES (?,?,?,?,?)',(nonce,uid,chat,time.time()+600,json.dumps(payload)))
        return nonce

    def consume(self, nonce, uid, chat):
        with self.db:
            row = self.db.execute('SELECT payload FROM intents WHERE nonce=? AND uid=? AND chat=? AND expires>?',(nonce,uid,chat,time.time())).fetchone()
            if row:
                self.db.execute('DELETE FROM intents WHERE nonce=?',(nonce,))
        return json.loads(row[0]) if row else None


def control(request):
    proc = subprocess.run(['/usr/bin/sudo','-n','--','/usr/local/sbin/smartdns-botctl'],
                          input=json.dumps(request), text=True, capture_output=True, timeout=170)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError('Не удалось вызвать управление SmartDNS. Проверьте сервис бота и sudoers на сервере.') from None
    if not result.get('ok'):
        raise RuntimeError(result.get('error','Ошибка управления SmartDNS.'))
    return result


class Bot:
    def __init__(self, api, state, admins, backend=control):
        self.api,self.state,self.admins,self.backend = api,state,set(admins),backend

    def send(self, chat, text, keyboard=MENU):
        for start in range(0,len(text),3500):
            args = {'chat_id':chat,'text':text[start:start+3500]}
            if start+3500>=len(text) and keyboard is not None:
                args['reply_markup'] = keyboard
            self.api.call('sendMessage',**args)

    def read(self, uid, action='list', **extra):
        return self.backend(dict(action=action, actor=uid, **extra))

    def listing(self, uid, chat, page=0, search=''):
        data = self.read(uid)
        values = [x for x in data['domains'] if search.casefold() in x.casefold()]
        page = max(0,min(page,max(0,(len(values)-1)//10)))
        text = ('Поиск: '+search+'\n' if search else 'Домены SmartDNS\n')+'\n'.join(values[page*10:page*10+10])
        text += '\n\nВсего: '+str(len(values))+'. Каждый домен включает поддомены.'
        keyboard = {'inline_keyboard':[]}
        if not search:
            buttons=[]
            if page:buttons.append({'text':'←','callback_data':'list:'+str(page-1)})
            if (page+1)*10<len(values):buttons.append({'text':'→','callback_data':'list:'+str(page+1)})
            if buttons:keyboard['inline_keyboard'].append(buttons)
        keyboard['inline_keyboard'] += MENU['inline_keyboard']
        self.send(chat,text,keyboard)

    def propose(self, uid, chat, action, text=''):
        current = self.read(uid)
        values = re.split(r'[\s,;]+',text.strip()) if text.strip() else []
        if action!='undo':
            # Import a sibling module only when launched with the installed -I
            # interpreter: explicitly load the root-owned local source.
            selected = sorted({normalize(x) for x in values})
            if not 1<=len(selected)<=50:
                raise ValueError('Отправьте от 1 до 50 доменов.')
            changed = sorted(set(selected)-set(current['domains']) if action=='add' else set(selected)&set(current['domains']))
            if not changed:
                self.send(chat,'Список уже в нужном состоянии. Изменений нет.');return
            description = ('Добавить:\n' if action=='add' else 'Удалить:\n')+'\n'.join(changed)
        else:
            selected=[];description='Вернуть список доменов из последней резервной копии?'
        payload={'action':action,'domains':selected,'version':current['version']}
        nonce=self.state.intent(uid,chat,payload)
        self.send(chat,description+'\n\nПрименить к DNS и TLS-шлюзу? Подтверждение действует 10 минут.',
                  {'inline_keyboard':[[{'text':'Применить','callback_data':'apply:'+nonce},{'text':'Отмена','callback_data':'cancel:'+nonce}]]})

    def handle(self, update):
        callback=update.get('callback_query')
        message=callback.get('message',{}) if callback else update.get('message',{})
        user=callback.get('from',{}) if callback else message.get('from',{})
        uid=user.get('id');chat=message.get('chat',{})
        if type(uid) is not int or uid not in self.admins or chat.get('type')!='private' or chat.get('id')!=uid:
            if callback:self.api.call('answerCallbackQuery',callback_query_id=callback['id'],text='Нет доступа.',show_alert=True)
            return
        chat=chat['id']
        if callback:
            self.api.call('answerCallbackQuery',callback_query_id=callback['id'])
        try:
            if callback:
                action=callback.get('data','')
                if action.startswith(('apply:','cancel:')):
                    kind,nonce=action.split(':',1);payload=self.state.consume(nonce,uid,chat)
                    if not payload:self.send(chat,'Подтверждение истекло или уже использовано. Повторите действие.');return
                    # Remove buttons before starting the privileged operation.
                    self.api.call('editMessageReplyMarkup',chat_id=chat,message_id=message['message_id'],reply_markup={'inline_keyboard':[]})
                    if kind=='cancel':self.send(chat,'Изменение отменено.');return
                    self.send(chat,'Применяю изменение…',None)
                    result=self.read(uid,**payload)
                    self.send(chat,('Изменение применено.' if result['changed'] else 'Изменений нет.')+' Домены: '+str(len(result['domains'])))
                    return
                if action.startswith('list:'):
                    self.state.prompt(uid,'');self.listing(uid,chat,int(action.split(':',1)[1]));return
            else:
                text=message.get('text','').strip()
                if not text:return
                if time.time()-message.get('date',0)>600:
                    self.send(chat,'Сообщение устарело. Повторите команду.');return
                command,*rest=text.split(maxsplit=1)
                if command.startswith('/'):
                    action=command[1:].split('@',1)[0]
                    argument=rest[0] if rest else ''
                else:
                    action=self.state.prompt(uid);argument=text
                if action in ('start','menu','cancel','help'):
                    self.state.prompt(uid,'');self.send(chat,'SmartDNS\nУправление доменами сервера. Добавление и удаление применяются после подтверждения.');return
                if action in ('list','domains'):
                    self.state.prompt(uid,'');self.listing(uid,chat);return
                if action in ('add','remove','search') and argument:
                    self.state.prompt(uid,'')
                    if action=='search':self.listing(uid,chat,search=argument[:253])
                    else:self.propose(uid,chat,action,argument)
                    return
            if action in ('add','remove','search'):
                self.state.prompt(uid,action)
                self.send(chat,'Отправьте '+('домен или часть имени для поиска.' if action=='search' else 'домены через пробел или с новой строки. Например: example.com\nПоддомены включаются автоматически.'))
            elif action=='undo':self.propose(uid,chat,'undo')
            elif action=='status':
                data=self.read(uid,'status')
                self.send(chat,'Сервисы сервера\n'+'\n'.join(name+': '+('работает' if active else 'НЕ РАБОТАЕТ') for name,active in data['services'].items())+'\nДомены: '+str(len(data['domains'])))
            else:self.send(chat,'Выберите действие в меню или используйте /add, /remove, /search, /list, /status, /undo.')
        except (ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            self.send(chat,'Не удалось выполнить действие:\n'+str(exc)[:2500])


# -I excludes the script directory from sys.path. Load only the fixed sibling;
# both installed sources are root-owned and not writable by the bot account.
import importlib.util
spec=importlib.util.spec_from_file_location('smartdns_control',Path(__file__).with_name('control.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
normalize=module.normalize


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    token=os.environ.get('TELEGRAM_BOT_TOKEN','')
    admins=os.environ.get('TELEGRAM_ADMIN_IDS','')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token) or not re.fullmatch(r'[1-9]\d*(?:,[1-9]\d*)*',admins):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS in the private environment file.')
    api=Telegram(token)
    api.call('getMe')
    if api.call('getWebhookInfo').get('url'):
        raise SystemExit('This token has an active webhook. Use a separate bot or remove its webhook explicitly.')
    state=State(Path(os.environ.get('STATE_DIRECTORY','/var/lib/smartdns-bot'))/'state.sqlite3')
    bot=Bot(api,state,map(int,admins.split(',')))
    running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    while running:
        try:
            for update in api.call('getUpdates',offset=state.offset(),timeout=25,allowed_updates=['message','callback_query']):
                bot.handle(update)
                state.offset(update['update_id']+1)
        except ApiError as exc:
            LOG.warning('Telegram response code %s',exc.code)
            if exc.code in (401,409):raise SystemExit('Invalid token or another bot instance is using it.') from None
            time.sleep(exc.retry)
        except Exception:
            # No URLs, tokens, or raw update bodies in logs.
            LOG.error('Update processing failed; restarting polling after 5 seconds')
            time.sleep(5)


if __name__=='__main__':main()
