import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'telegram-bot'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
control=load('control');bot=load('bot')


class DomainTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(control.normalize(' *.Example.COM. '),'example.com')
        self.assertEqual(control.normalize('пример.рф'),'xn--e1afmkfd.xn--p1ai')
    def test_reject_invalid_and_injection(self):
        for value in ['com','https://example.com','example.com/a','127.0.0.1','a.com:443','a..com','-a.com','a_.com','a.com\nroot','x.com";os.execute("id")','a'*64+'.com']:
            with self.subTest(value=value),self.assertRaises((ValueError,UnicodeError)):control.normalize(value)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.files=tuple(self.root/name for name in ('domains.txt','dns.conf','nginx.conf'))
        self.original=(b'# hand edited\nexample.com\nkeep.org\n',b'original dns',b'original nginx')
        for path,data in zip(self.files,self.original):path.write_bytes(data);path.chmod(0o640)
        self.calls=[];self.fail_rebuild=False;self.fail_restore=False
        def run(args,timeout=60):
            self.calls.append(args)
            if args==[control.REBUILD]:
                self.files[1].write_text('new dns');self.files[2].write_text('new nginx')
                if self.fail_rebuild:raise RuntimeError('nginx rejected candidate')
            if self.fail_restore and args[:2]==['/usr/sbin/nginx','-t']:raise RuntimeError('rollback failed')
        self.runner=run;self.state=self.root/'backups'
        self.c=control.Controller(self.files,self.state,run)
    def tearDown(self):self.tmp.cleanup()
    def request(self,action,values=()):return {'actor':123,'action':action,'domains':list(values),'version':self.c.read()['version']}
    def test_add_and_remove(self):
        result=self.c.handle(self.request('add',['new.org']))
        self.assertTrue(result['changed']);self.assertIn('new.org',result['domains'])
        result=self.c.handle(self.request('remove',['example.com']))
        self.assertEqual(result['domains'],['keep.org','new.org'])
        self.assertEqual(self.files[0].stat().st_mode&0o777,0o640)
    def test_noop_does_not_restart(self):
        self.assertFalse(self.c.handle(self.request('add',['example.com']))['changed']);self.assertEqual(self.calls,[])
    def test_stale_confirmation(self):
        request=self.request('add',['new.org']);self.files[0].write_text('changed.org\n')
        with self.assertRaisesRegex(ValueError,'уже изменился'):self.c.handle(request)
        self.assertEqual(self.calls,[])
    def test_empty_list_refused(self):
        with self.assertRaisesRegex(ValueError,'все домены'):self.c.handle(self.request('remove',['example.com','keep.org']))
        self.assertEqual(self.files[0].read_bytes(),self.original[0])
    def test_failed_apply_restores_exact_files(self):
        self.fail_rebuild=True
        with self.assertRaisesRegex(RuntimeError,'восстановлены'):self.c.handle(self.request('add',['new.org']))
        self.assertEqual(tuple(p.read_bytes() for p in self.files),self.original)
        self.assertFalse((self.state/'pending.json').exists())
        self.assertIn(['/usr/bin/systemctl','restart','dnsdist','nginx'],self.calls)
    def test_failed_rollback_retains_recovery_marker(self):
        self.fail_rebuild=True;self.fail_restore=True
        with self.assertRaisesRegex(RuntimeError,'КРИТИЧНО'):self.c.handle(self.request('add',['new.org']))
        self.assertTrue((self.state/'pending.json').exists())
        self.fail_restore=False;self.fail_rebuild=False
        self.c.handle({'action':'list','actor':123})
        self.assertFalse((self.state/'pending.json').exists())
        self.assertEqual(tuple(p.read_bytes() for p in self.files),self.original)
    def test_recovery_after_interrupted_write(self):
        self.state.mkdir();saved={'snapshot':self.c.snapshot()}
        (self.state/'pending.json').write_text(json.dumps(saved))
        for p in self.files:p.write_text('partial changes')
        result=self.c.handle({'actor':123,'action':'list'})
        self.assertEqual(result['domains'],['example.com','keep.org'])
        self.assertEqual(tuple(p.read_bytes() for p in self.files),self.original)
    def test_undo_and_redo(self):
        self.c.handle(self.request('add',['new.org']))
        self.assertNotIn('new.org',self.c.handle(self.request('undo'))['domains'])
        self.assertIn('new.org',self.c.handle(self.request('undo'))['domains'])
    def test_failed_attempt_does_not_hide_last_successful_undo(self):
        self.c.handle(self.request('add',['new.org']))
        self.fail_rebuild=True
        with self.assertRaises(RuntimeError):self.c.handle(self.request('add',['failure.org']))
        self.fail_rebuild=False
        self.assertNotIn('new.org',self.c.handle(self.request('undo'))['domains'])
    def test_invalid_operation_and_actor(self):
        for request in ({'action':'shell','actor':123},{'action':'add','actor':True},{'action':'list'}):
            with self.assertRaises(ValueError):self.c.handle(request)
        self.assertEqual(self.calls,[])


class FakeApi:
    def __init__(self):self.calls=[]
    def call(self,method,**kwargs):self.calls.append((method,kwargs));return {}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=FakeApi();self.state=bot.State(Path(self.tmp.name)/'state.db');self.requests=[]
        def backend(request):
            self.requests.append(request)
            return {'ok':True,'domains':['example.com'],'version':'a'*64,'changed':True}
        self.b=bot.Bot(self.api,self.state,[123,456],backend)
    def tearDown(self):self.state.db.close();self.tmp.cleanup()
    def message(self,text,uid=123,kind='private'):
        return {'message':{'from':{'id':uid},'chat':{'id':uid,'type':kind},'text':text,'date':int(time.time())}}
    def callback(self,data,uid=123):
        return {'callback_query':{'id':'query','from':{'id':uid},'data':data,'message':{'message_id':1,'chat':{'id':uid,'type':'private'}}}}
    def test_unauthorized_and_group_updates_never_reach_backend(self):
        self.b.handle(self.message('/add evil.com',999));self.b.handle(self.message('/add evil.com',kind='group'))
        self.b.handle(self.callback('status',999));self.assertEqual(self.requests,[])
    def test_proposal_does_not_apply(self):
        self.b.handle(self.message('/add new.org'))
        self.assertEqual([r['action'] for r in self.requests],['list'])
        self.assertTrue(any('Применить' in json.dumps(kwargs,ensure_ascii=False) for _,kwargs in self.api.calls))
    def test_confirm_once_and_bound_to_admin(self):
        self.b.handle(self.message('/add new.org'))
        markup=self.api.calls[-1][1]['reply_markup'];data=markup['inline_keyboard'][0][0]['callback_data']
        self.b.handle(self.callback(data,456));self.assertEqual(len(self.requests),1)
        self.b.handle(self.callback(data));self.b.handle(self.callback(data))
        self.assertEqual([r['action'] for r in self.requests],['list','add'])
    def test_cancel_does_not_apply(self):
        self.b.handle(self.message('/add new.org'))
        data=self.api.calls[-1][1]['reply_markup']['inline_keyboard'][0][1]['callback_data']
        self.b.handle(self.callback(data));self.assertEqual([r['action'] for r in self.requests],['list'])
    def test_expired_intent_and_offset_persist(self):
        nonce=self.state.intent(123,123,{'action':'add'})
        self.state.db.execute('UPDATE intents SET expires=0');self.state.db.commit()
        self.assertIsNone(self.state.consume(nonce,123,123))
        self.state.offset(999);self.assertEqual(self.state.offset(),999)
    def test_prompt_and_bulk_input(self):
        self.b.handle(self.callback('add'));self.b.handle(self.message('one.org\ntwo.org'))
        self.assertEqual(self.requests[0]['action'],'list')
        row=self.state.db.execute('SELECT payload FROM intents').fetchone()
        self.assertEqual(json.loads(row[0])['domains'],['one.org','two.org'])
    def test_injection_never_applied(self):
        self.b.handle(self.message('/add a.com;rm -rf /'))
        self.assertEqual([r['action'] for r in self.requests],['list'])
        self.assertIsNone(self.state.db.execute('SELECT payload FROM intents').fetchone())


if __name__=='__main__':unittest.main()
