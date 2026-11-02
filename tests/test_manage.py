import os
import subprocess
import sys

from app.state import state_lock
from app.store import Store, password_hash, password_matches


def run_reset(folder, *args):
    return subprocess.run([sys.executable,'-m','app.manage',*args],
                          env=dict(os.environ,APP_DATA=str(folder)),text=True,capture_output=True)


def test_reset_requires_explicit_flag(tmp_path):
    folder=tmp_path/'config'
    result=run_reset(folder,'reset-all')
    assert result.returncode==2 and not folder.exists()


def test_reset_admin_removes_credentials_and_totp_preserves_preferences(tmp_path):
    folder=tmp_path/'config'
    db=Store(str(folder))
    db.update(username='olduser',password=password_hash('old-password'),must_change=0,totp='OLDKEY',last_step=123,
              webhook='https://discord.com/api/webhooks/123/secret',events='error',
              telegram_token='123456:TEST_ONLY_token_123456789012345',telegram_chat_id='-1001234567890')
    db.log('info','old log');db.db.close()
    assert run_reset(folder,'reset-admin','--yes').returncode==0
    db=Store(str(folder));c=db.config()
    assert c['username']=='admin' and password_matches('admin',c['password'])
    assert c['must_change']==1 and c['totp']=='' and c['last_step']==-1
    assert c['events']=='error' and c['webhook'].endswith('/secret')
    assert c['telegram_token']=='123456:TEST_ONLY_token_123456789012345' and c['telegram_chat_id']=='-1001234567890'
    assert any(row['message']=='old log' for row in db.logs())
    db.db.close()


def test_reset_all_clears_state_but_does_not_touch_media(tmp_path):
    folder=tmp_path/'config';media=tmp_path/'media';media.mkdir()
    sample=media/'한글_日本語_漢字.mp4';sample.write_bytes(b'unchanged')
    db=Store(str(folder))
    db.update(username='olduser',password=password_hash('old-password'),must_change=0,totp='OLDKEY',last_step=123,
              webhook='https://discord.com/api/webhooks/123/secret',events='error',
              telegram_token='123456:TEST_ONLY_token_123456789012345',telegram_chat_id='-1001234567890')
    db.log('info','old log');db.db.close()
    result=subprocess.run([sys.executable,'-m','app.manage','reset-all','--yes'],
        env=dict(os.environ,APP_DATA=str(folder),MEDIA_ROOT=str(media)),text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    db=Store(str(folder));c=db.config()
    assert c['username']=='admin' and password_matches('admin',c['password'])
    assert c['must_change']==1 and c['totp']=='' and c['last_step']==-1 and c['webhook']==''
    assert c['events']=='start,complete,error'
    assert c['telegram_token']==c['telegram_chat_id']==''
    assert [row['message'] for row in db.logs()]==['전체 설정 초기화']
    assert list(media.iterdir())==[sample] and sample.read_bytes()==b'unchanged'
    db.db.close()


def test_reset_refuses_running_service_state(tmp_path):
    folder=tmp_path/'config';db=Store(str(folder));db.update(username='keepme');db.db.close()
    with state_lock(str(folder)):
        result=run_reset(folder,'reset-all','--yes')
        assert result.returncode==1 and '서비스가 실행 중' in result.stderr
    db=Store(str(folder));assert db.config()['username']=='keepme';db.db.close()


def test_change_history_persistence_pagination_and_reset(tmp_path):
    folder=tmp_path/'config'; db=Store(str(folder))
    db.begin_job('job1','한글')
    for i in range(205): db.record_change('job1','/한글',f'old{i}',f'new{i}',i)
    db.finish_job('job1','completed');db.db.close()
    db=Store(str(folder))
    first=db.changes();assert first['total']==205 and len(first['rows'])==200
    second=db.changes(before=first['rows'][-1]['id'])
    assert len(second['rows'])==5
    assert not {r['id'] for r in first['rows']} & {r['id'] for r in second['rows']}
    db.reset();assert db.changes()['total']==205
    db.reset(all_settings=True);assert db.changes()['rows']==[] and db.changes()['job'] is None
    db.db.close()


def test_upgrade_creates_changes_tables_preserving_existing_config(tmp_path):
    import sqlite3
    folder=tmp_path/'oldconfig';folder.mkdir()
    con=sqlite3.connect(folder/'app.sqlite3')
    con.execute('CREATE TABLE config (id INTEGER PRIMARY KEY,username TEXT,password TEXT,must_change INTEGER,totp TEXT,last_step INTEGER,webhook TEXT,events TEXT)')
    hashed=password_hash('keep-password')
    con.execute('INSERT INTO config VALUES(1,?,?,0,?,123,?,?)',('existing',hashed,'EXISTINGKEY','https://discord.com/api/webhooks/1/token','error'))
    con.commit();con.close()
    db=Store(str(folder));config=db.config()
    assert config['username']=='existing' and config['password']==hashed and config['totp']=='EXISTINGKEY'
    assert config['webhook'].endswith('/token') and config['events']=='error'
    assert config['telegram_token']==config['telegram_chat_id']==''
    db.db.close()
    db=Store(str(folder));config=db.config()
    assert config['password']==hashed and config['telegram_token']==config['telegram_chat_id']==''
    assert db.changes()=={'job':None,'total':0,'rows':[]}
    db.db.close()
