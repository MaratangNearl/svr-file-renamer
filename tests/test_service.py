import os
import threading
import time
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.files import Volume
from app.store import password_hash
import app.main as main

HEADERS = {'X-Requested-With': 'adname-ui'}


@pytest.fixture(scope='module')
def service(tmp_path_factory):
    root = tmp_path_factory.mktemp('media')
    main.ROOT = str(root)
    main.DATA = str(tmp_path_factory.mktemp('config'))
    with TestClient(main.app, headers=HEADERS) as client:
        yield client, root


@pytest.fixture
def client(service):
    client, _ = service
    with main.auth_lock:
        main.sessions.clear()
        main.failures.clear()
    main.store.update(username='admin', password=password_hash('test-password'), must_change=0, totp='', last_step=-1, webhook='', events='start,complete,error')
    client.cookies.clear()
    assert client.post('/api/login', json={'username':'admin', 'password':'test-password'}).status_code == 200
    return client


def wait_job(client):
    for _ in range(500):
        job = client.get('/api/jobs').json()
        if job['state'] not in main.ACTIVE:
            return job
        time.sleep(.01)
    pytest.fail('job did not finish')


def test_first_login_gate(client):
    main.store.update(password=password_hash('admin'), must_change=1)
    assert client.get('/api/folders').status_code == 403
    assert client.post('/api/jobs', json={'find':'abc'}).status_code == 403
    assert client.post('/api/totp/setup').status_code == 403
    assert client.put('/api/settings', json={'username':'admin','password':'admin'}).status_code == 400
    assert client.post('/api/account/setup', json={'username':'newadmin','password':'new-password-123'}).status_code == 200
    assert client.get('/api/folders').status_code == 200
    assert main.store.config()['must_change'] == 0
    assert main.store.config()['password'] != 'new-password-123'


def test_auth_and_csrf(client):
    assert client.post('/api/logout', headers={'X-Requested-With':''}).status_code == 403
    assert client.post('/api/logout', headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/logout', headers={'Origin':'https://testserver'}).status_code == 200
    assert client.get('/api/jobs').status_code == 401


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/login', json={'username':'admin','password':'wrong'}).status_code == 401
    assert client.post('/api/login', json={'username':'admin','password':'test-password'}).status_code == 429


def test_volume_boundaries(client, service, tmp_path):
    _, root = service
    outside = tmp_path / 'outside'; outside.mkdir(); (outside/'private.mp4').touch()
    (root/'escape').symlink_to(outside, target_is_directory=True)
    assert client.get('/api/folders?path=../').status_code == 400
    assert client.get('/api/folders?path=escape').status_code == 400
    assert 'escape' not in client.get('/api/folders').json()['folders']
    assert client.post('/api/jobs', json={'folder':'../','find':'private'}).status_code == 400
    assert client.post('/api/jobs', json={'folder':'escape','find':'private'}).status_code == 400


def test_examples_and_collisions(client, service):
    _, root = service
    folder = root/'examples'; folder.mkdir()
    (folder/'sub').mkdir()
    for name in ['abcd.com@movie1.mp4','movie1.mp4','movie1_1.mp4','anime-1#addomain.cn.mkv','anime-2.mkv']:
        (folder/name).write_text(name)
    (folder/'sub'/'abcd.com@movie2.mp4').touch()
    for find, replace, count in [('abcd.com@','',2),('#addomain.cn','',1),('anime-','anime#',2)]:
        assert client.post('/api/jobs', json={'folder':'examples','find':find,'replace':replace}).status_code == 200
        job = wait_job(client)
        assert job['state'] == 'completed'
        assert job['renamed'] == count and job['errors'] == 0 and job['percent'] == 100
    assert (folder/'movie1_2.mp4').read_text() == 'abcd.com@movie1.mp4'
    assert (folder/'movie1.mp4').read_text() == 'movie1.mp4'
    assert (folder/'anime#1.mkv').exists() and (folder/'anime#2.mkv').exists()
    assert (folder/'sub'/'movie2.mp4').exists()


def test_collision_number_not_nested(tmp_path):
    for name in ['ad_movie1_1.mp4','movie1_1.mp4','movie1_2.mp4']:
        (tmp_path/name).touch()
    v = Volume(str(tmp_path))
    try:
        info = (tmp_path/'ad_movie1_1.mp4').stat()
        result = v.rename('ad_movie1_1.mp4','movie1_1.mp4',(info.st_dev,info.st_ino))
        assert result == 'movie1_3.mp4'
    finally:
        os.close(v.fd)


def test_symlink_swap_is_rejected(tmp_path):
    (tmp_path/'file').touch()
    original = (tmp_path/'file').stat()
    (tmp_path/'file').unlink()
    (tmp_path/'file').symlink_to('/etc/passwd')
    v = Volume(str(tmp_path))
    try:
        with pytest.raises(ValueError):
            v.rename('file','new',(original.st_dev, original.st_ino))
    finally:
        os.close(v.fd)


def test_invalid_result_logged(client, service):
    _, root = service
    folder=root/'invalid';folder.mkdir();(folder/'remove').touch()
    assert client.post('/api/jobs',json={'folder':'invalid','find':'remove'}).status_code==200
    job=wait_job(client)
    assert job['errors']==1 and job['renamed']==0
    assert (folder/'remove').exists()
    assert any('비어' in row['message'] for row in client.get('/api/logs').json())


def test_stop_and_concurrent_job(client, service, monkeypatch):
    _, root=service
    folder=root/'stop';folder.mkdir()
    for n in range(20): (folder/f'ad_{n}.mp4').touch()
    entered=threading.Event(); release=threading.Event(); original=main.volume.rename
    def slow(*args):
        entered.set(); release.wait(3); return original(*args)
    monkeypatch.setattr(main.volume,'rename',slow)
    try:
        assert client.post('/api/jobs',json={'folder':'stop','find':'ad_'}).status_code==200
        assert entered.wait(3)
        assert client.post('/api/jobs',json={'folder':'stop','find':'ad_'}).status_code==409
        assert client.post('/api/jobs/stop').json()['state']=='stopping'
    finally:
        release.set()
    job=wait_job(client)
    assert job['state']=='stopped' and job['processed']<job['total']


def test_totp_optional_enrollment_and_replay(client):
    assert client.get('/api/settings').json()['totp_enabled'] is False
    setup=client.post('/api/totp/setup').json()
    assert setup['qr'].startswith('data:image/png;base64,')
    assert main.store.config()['totp']==''
    assert client.post('/api/totp/enable',json={'code':'bad'}).status_code==400
    code=pyotp.TOTP(setup['key']).now()
    assert client.post('/api/totp/enable',json={'code':code}).status_code==200
    assert client.get('/api/settings').json()['totp_enabled'] is True
    assert client.post('/api/login',json={'username':'admin','password':'test-password'}).status_code==401
    assert client.post('/api/login',json={'username':'admin','password':'test-password','code':code}).status_code==401
    # Simulate the next authentication window without a 30-second wait.
    main.store.update(last_step=int(time.time()//30)-1)
    assert client.post('/api/login',json={'username':'admin','password':'test-password','code':code}).status_code==200
    main.store.update(last_step=int(time.time()//30)-1)
    assert client.post('/api/totp/disable',json={'code':code}).status_code==200
    assert main.store.config()['totp']==''


def test_webhook_validation_and_delivery(client, monkeypatch):
    assert client.put('/api/settings',json={'username':'admin','webhook':'http://127.0.0.1/internal'}).status_code==400
    assert client.post('/api/webhook/test',json={'webhook':'https://evil.example/test'}).status_code==400
    seen=[]
    def fake_post(self,url,**kwargs):
        seen.append((url,kwargs));return main.httpx.Response(204)
    monkeypatch.setattr(main.httpx.Client,'post',fake_post)
    url='https://discord.com/api/webhooks/123456/token'
    assert client.request('POST','/api/webhook/test',json={'webhook':url}).status_code==200
    assert seen[0][1]['json']['allowed_mentions']=={'parse':[]}


def test_webhook_preferences_and_secrecy(client):
    url='https://discord.com/api/webhooks/123456/secret'
    assert client.put('/api/settings',json={'username':'admin','webhook':url,'events':['error']}).status_code==200
    s=client.get('/api/settings').json()
    assert s['events']==['error'] and s['webhook_configured'] is True
    assert 'secret' not in str(s) and 'password' not in s


def test_static_security_headers(client):
    r=client.get('/')
    assert r.status_code==200
    assert r.headers['X-Frame-Options']=='DENY'
    assert "default-src 'self'" in r.headers['Content-Security-Policy']


def test_defaults_and_persistence(tmp_path):
    from app.store import Store, password_matches
    db=Store(str(tmp_path/'config'))
    config=db.config()
    assert config['username']=='admin' and config['must_change']==1 and config['totp']==''
    assert password_matches('admin',config['password'])
    db.update(username='persisted',events='error')
    db.log('info','persistent event')
    db.db.close()
    reopened=Store(str(tmp_path/'config'))
    assert reopened.config()['username']=='persisted'
    assert reopened.logs()[0]['message']=='persistent event'
    reopened.db.close()


def test_discord_event_filtering(client, monkeypatch):
    sent=[]
    main.store.update(webhook='https://discord.com/api/webhooks/123/token',events='error')
    monkeypatch.setattr(main,'send_discord',lambda url,message:sent.append(message))
    main.notify('start','skip-start'); main.notify('complete','skip-complete'); main.notify('error','send-error')
    main.notifications.submit(lambda:None).result(timeout=3)
    assert sent==['send-error']


def test_discord_failure_is_logged(client, monkeypatch):
    main.store.update(webhook='https://discord.com/api/webhooks/123/hidden-token',events='error')
    def fail(*args): raise ValueError('network')
    monkeypatch.setattr(main,'send_discord',fail)
    main.notify('error','error')
    main.notifications.submit(lambda:None).result(timeout=3)
    rows=client.get('/api/logs').json()
    assert any('Discord 알림 전송 실패' in row['message'] for row in rows)
    assert all('hidden-token' not in row['message'] for row in rows)


@pytest.mark.parametrize(('name','find','replace','expected','errors'), [
    ('anime-1#addomain.cn.mkv','\u200b#addomain.cn\u200b','','anime-1.mkv',0),
    ('anime-1.mkv','\u200b\u200banime-\u200b','anime@','anime@1.mkv',0),
    ('remove-me','remove-me\u200b','','remove-me',1),
    ('ad_movie.mp4','\ufeffad_\u2060','\u200bnew_\ufeff','new_movie.mp4',0),
    ('a b.mp4',' ','_','a_b.mp4',0),
    ('a\u200bb.mp4','a\u200bb','ok','ok.mp4',0),
])
def test_pasted_rule_edges(client, service, tmp_path, name, find, replace, expected, errors):
    _, root=service
    folder=root/('paste-'+tmp_path.name);folder.mkdir();(folder/name).touch()
    assert client.post('/api/jobs',json={'folder':folder.name,'find':find,'replace':replace}).status_code==200
    job=wait_job(client)
    assert job['total']==1 and job['processed']==1 and job['errors']==errors
    assert (folder/expected).exists()


def test_empty_invisible_rule_rejected(client):
    for value in ['\u200b','\ufeff\u2060\u200b']:
        assert client.post('/api/jobs',json={'find':value}).status_code==400
    assert client.post('/api/jobs',json={'find':'\u200banime-','replace':'anime-\u200b'}).status_code==400


def test_authenticated_settings_need_no_reauthentication(client):
    secret=pyotp.random_base32()
    main.store.update(totp=secret,last_step=int(time.time()//30))
    assert client.put('/api/settings',json={'username':'admin','events':['complete']}).status_code==200
    assert main.store.config()['events']=='complete'
    assert client.put('/api/settings',json={'username':'new-name','password':'new-password-123'}).status_code==200
    assert client.post('/api/totp/disable').status_code==200
    assert main.store.config()['totp']==''
    assert client.post('/api/logout').status_code==200
    assert client.put('/api/settings',json={'username':'new-name'}).status_code==401
    assert client.post('/api/totp/disable').status_code==401
    assert client.post('/api/login',json={'username':'new-name','password':'new-password-123'}).status_code==200


def test_initial_account_only_changes_credentials(client):
    main.store.update(must_change=1)
    assert client.put('/api/settings',json={'username':'admin','password':'new-password-123','events':[]}).status_code==403
    assert client.post('/api/account/setup',json={'username':'','password':'new-password-123'}).status_code==422
    assert client.post('/api/account/setup',json={'username':'newadmin','password':'new-password-123'}).status_code==200
    assert main.store.config()['events']=='start,complete,error'
    assert client.post('/api/account/setup',json={'username':'newadmin','password':'new-password-123'}).status_code==409


@pytest.mark.parametrize('with_file', [False, True])
def test_no_work_ui_state_logs_and_discord(client, service, tmp_path, monkeypatch, with_file):
    _, root=service
    folder=root/('no-work-'+tmp_path.name);folder.mkdir()
    if with_file: (folder/'keep.mp4').touch()
    messages=[]
    main.store.update(webhook='https://discord.com/api/webhooks/123/test',events='complete')
    monkeypatch.setattr(main,'send_discord',lambda url,message:messages.append(message))
    assert client.post('/api/jobs',json={'folder':folder.name,'find':'not-present'}).status_code==200
    job=wait_job(client)
    assert job['state']=='no_work'
    assert job['processed']==job['total']==job['renamed']==job['errors']==job['percent']==0
    main.notifications.submit(lambda:None).result(timeout=3)
    assert len(messages)==1 and '수행할 작업이 없습니다' in messages[0]
    assert '작업 완료' not in messages[0]
    log=client.get('/api/logs').json()[0]['message']
    assert '수행할 작업이 없습니다' in log and '작업 완료' not in log


def test_scan_error_is_not_no_work(client, service, tmp_path, monkeypatch):
    _, root=service
    folder=root/('scan-error-'+tmp_path.name);folder.mkdir()
    def failed_walk(path, stop, on_error):
        on_error(path, PermissionError('permission denied'))
        return iter(())
    monkeypatch.setattr(main.volume,'walk',failed_walk)
    assert client.post('/api/jobs',json={'folder':folder.name,'find':'abc'}).status_code==200
    job=wait_job(client)
    assert job['state']=='completed' and job['errors']==1


@pytest.mark.parametrize(('original','find','replace','expected'), [
    ('광고@한글 영화 1.mp4','광고@','','한글 영화 1.mp4'),
    ('광고@한글 영화 1.mp4','한글 영화','새로운 제목','광고@새로운 제목 1.mp4'),
    ('広告@アニメ第１話.mkv','広告@','','アニメ第１話.mkv'),
    ('アニメ第１話.mkv','アニメ','映画','映画第１話.mkv'),
    ('廣告@漢字電影第一集.mp4','廣告@','','漢字電影第一集.mp4'),
    ('漢字電影第一集.mp4','電影','影片','漢字影片第一集.mp4'),
    ('광고@日本語_漢字_한글 🎬.mkv','\u200b광고@\u200b','','日本語_漢字_한글 🎬.mkv'),
])
def test_multilingual_names_and_folders(client, service, tmp_path, original, find, replace, expected):
    _, root=service
    folder=root/('한글_日本語_漢字_'+tmp_path.name);folder.mkdir()
    (folder/original).write_text('내용・内容・內容',encoding='utf-8')
    assert folder.name in client.get('/api/folders').json()['folders']
    assert client.post('/api/jobs',json={'folder':folder.name,'find':find,'replace':replace}).status_code==200
    job=wait_job(client)
    assert job['renamed']==1 and job['errors']==0
    assert (folder/expected).read_text(encoding='utf-8')=='내용・内容・內容'
    assert any(original in row['message'] and expected in row['message'] for row in client.get('/api/logs').json())


def test_multilingual_collision(client, service):
    _, root=service
    folder=root/'다국어_衝突';folder.mkdir()
    for name in ['광고@한글_日本語_漢字.mkv','한글_日本語_漢字.mkv','한글_日本語_漢字_1.mkv']:
        (folder/name).write_text(name,encoding='utf-8')
    assert client.post('/api/jobs',json={'folder':folder.name,'find':'광고@'}).status_code==200
    assert wait_job(client)['errors']==0
    assert (folder/'한글_日本語_漢字_2.mkv').read_text(encoding='utf-8')=='광고@한글_日本語_漢字.mkv'


def test_change_details_includes_path_names_and_size(client, service):
    _,root=service;folder=root/'details';folder.mkdir()
    (folder/'광고@한글_日本語_漢字.mp4').write_bytes(b'123456789')
    (folder/'한글_日本語_漢字.mp4').write_bytes(b'keep')
    assert client.post('/api/jobs',json={'folder':'details','find':'광고@'}).status_code==200
    job=wait_job(client)
    result=client.get('/api/changes').json()
    assert result['total']==1 and result['job']['id']==job['id']
    row=result['rows'][0]
    assert row['path']=='/details' and row['old_name']=='광고@한글_日本語_漢字.mp4'
    assert row['new_name']=='한글_日本語_漢字_1.mp4' and row['size_bytes']==9
    assert (folder/row['new_name']).read_bytes()==b'123456789'
    # A subsequent job with no changes must not show earlier changes as its own.
    assert client.post('/api/jobs',json={'folder':'details','find':'missing'}).status_code==200
    assert wait_job(client)['state']=='no_work'
    assert client.get('/api/changes').json()['rows']==[]
    assert any(r['id']==row['id'] for r in client.get('/api/changes?all_jobs=true').json()['rows'])


def test_changes_require_auth(client):
    client.post('/api/logout')
    assert client.get('/api/changes').status_code==401
