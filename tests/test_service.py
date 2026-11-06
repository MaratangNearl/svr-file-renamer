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
def client(service, monkeypatch):
    client, _ = service
    monkeypatch.setattr(main.telegram_bot, 'poll_once', lambda: None)
    with main.auth_lock:
        main.sessions.clear()
        main.failures.clear()
    main.store.update(username='admin', password=password_hash('test-password'), must_change=0, totp='', last_step=-1, webhook='', telegram_token='', telegram_chat_id='', telegram_commands=0, events='start,complete,error')
    client.cookies.clear()
    assert client.post('/api/login', json={'username':'admin', 'password':'test-password'}).status_code == 200
    yield client
    main.store.update(telegram_commands=0)


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


TELEGRAM_TOKEN = '123456:TEST_ONLY_token_123456789012345'


def test_telegram_test_delivery_and_no_automatic_save(client, monkeypatch):
    seen = []
    def fake_post(self, url, **kwargs):
        seen.append((url, kwargs))
        return main.httpx.Response(200, json={'ok': True, 'result': {}})
    monkeypatch.setattr(main.httpx.Client, 'post', fake_post)
    assert client.request('POST', '/api/telegram/test', json={
        'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '-1001234567890'}).status_code == 200
    assert seen[0][0] == f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = seen[0][1]['json']
    assert payload['chat_id'] == '-1001234567890' and '연결 테스트 성공' in payload['text']
    assert 'parse_mode' not in payload and payload['link_preview_options']['is_disabled']
    assert main.store.config()['telegram_token'] == ''


def test_telegram_settings_preserve_replace_delete_and_secrecy(client, monkeypatch):
    values = {'username': 'admin', 'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '123456789'}
    assert client.put('/api/settings', json=values).status_code == 200
    assert client.put('/api/settings', json={'username': 'admin', 'events': ['error']}).status_code == 200
    assert main.store.config()['telegram_token'] == TELEGRAM_TOKEN
    s = client.get('/api/settings').json()
    assert s['telegram_configured'] and s['events'] == ['error']
    assert TELEGRAM_TOKEN not in str(s) and 'telegram_token' not in s
    seen = []
    monkeypatch.setattr(main, 'send_telegram', lambda *args: seen.append(args))
    assert client.post('/api/telegram/test', json={}).status_code == 200
    assert seen[0][:2] == (TELEGRAM_TOKEN, '123456789')
    assert client.put('/api/settings', json={'username': 'admin', 'telegram_chat_id': '@my_channel'}).status_code == 200
    assert main.store.config()['telegram_chat_id'] == '@my_channel'
    assert client.put('/api/settings', json={'username': 'admin', 'telegram_token': '', 'telegram_chat_id': ''}).status_code == 200
    assert not client.get('/api/settings').json()['telegram_configured']
    assert main.store.config()['telegram_token'] == main.store.config()['telegram_chat_id'] == ''


@pytest.mark.parametrize(('token', 'chat_id'), [
    ('bad', '123'), (TELEGRAM_TOKEN + '/getMe', '123'),
    (TELEGRAM_TOKEN, 'https://evil.example'), (TELEGRAM_TOKEN, ''), ('', '123'),
])
def test_telegram_invalid_settings_and_test_rejected(client, token, chat_id):
    values = {'telegram_token': token, 'telegram_chat_id': chat_id}
    assert client.put('/api/settings', json={'username': 'admin', **values}).status_code == 400
    assert client.post('/api/telegram/test', json=values).status_code == 400
    assert main.store.config()['telegram_token'] == ''


@pytest.mark.parametrize('response', [
    main.httpx.Response(403, json={'ok': False, 'description': TELEGRAM_TOKEN}),
    main.httpx.Response(200, json={'ok': False, 'description': TELEGRAM_TOKEN}),
    main.httpx.Response(200, text=TELEGRAM_TOKEN),
])
def test_telegram_api_failures_hide_response_secrets(client, monkeypatch, response):
    monkeypatch.setattr(main.httpx.Client, 'post', lambda *args, **kwargs: response)
    result = client.request('POST', '/api/telegram/test', json={
        'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '123456789'})
    assert result.status_code == 400 and TELEGRAM_TOKEN not in result.text


def test_telegram_connection_error_hides_token(client, monkeypatch):
    def fail(*args, **kwargs):
        raise main.httpx.ConnectError(f'failed: {TELEGRAM_TOKEN}')
    monkeypatch.setattr(main.httpx.Client, 'post', fail)
    result = client.request('POST', '/api/telegram/test', json={
        'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '123456789'})
    assert result.status_code == 400 and TELEGRAM_TOKEN not in result.text


def test_telegram_authentication_and_initial_setup_gate(client):
    values = {'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '123456789'}
    main.store.update(must_change=1)
    assert client.post('/api/telegram/test', json=values).status_code == 403
    assert client.post('/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN}).status_code == 403
    assert client.put('/api/settings', json={'username': 'admin', **values}).status_code == 403
    assert client.post('/api/logout').status_code == 200
    assert client.post('/api/telegram/test', json=values).status_code == 401
    assert client.post('/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN}).status_code == 401


@pytest.mark.parametrize('saved_token', [False, True])
def test_telegram_chat_discovery_filters_deduplicates_and_does_not_save_or_consume(client, monkeypatch, saved_token):
    if saved_token:
        main.store.update(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='555')
    before = main.store.config()
    seen = []
    def fake_post(self, url, **kwargs):
        method = url.rsplit('/', 1)[-1]
        seen.append((method, kwargs['json']))
        assert url.startswith(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/')
        if method == 'getMe':
            result = {'id': 123456, 'username': 'renamer_test_bot', 'is_bot': True}
        else:
            assert method == 'getUpdates'
            result = [
                {'update_id': 1, 'message': {'chat': {'id': 987, 'type': 'private', 'first_name': '홍길동'}, 'text': '/start'}},
                {'update_id': 2, 'message': {'chat': {'id': 987, 'type': 'private', 'first_name': '홍길동'}, 'text': 'private text'}},
                {'update_id': 3, 'message': {'chat': {'id': -10099, 'type': 'supergroup', 'title': '<그룹>'}, 'from': {'id': 123}}},
                {'update_id': 4, 'channel_post': {'chat': {'id': -10088, 'type': 'channel', 'title': '공지'}}},
                {'update_id': 5, 'my_chat_member': {'chat': {'id': -77, 'type': 'group', 'title': '가족'}}},
                {'update_id': 6, 'inline_query': {'from': {'id': 333, 'first_name': '수신 대화 아님'}}},
            ]
        return main.httpx.Response(200, json={'ok': True, 'result': result})
    monkeypatch.setattr(main.httpx.Client, 'post', fake_post)
    result = client.request('POST', '/api/telegram/chats', json={} if saved_token else {'telegram_token': TELEGRAM_TOKEN})
    assert result.status_code == 200
    assert result.json() == {'bot_username': 'renamer_test_bot', 'chats': [
        {'id': '987', 'type': 'private', 'name': '홍길동'},
        {'id': '-10099', 'type': 'supergroup', 'name': '<그룹>'},
        {'id': '-10088', 'type': 'channel', 'name': '공지'},
        {'id': '-77', 'type': 'group', 'name': '가족'},
    ]}
    assert seen == [('getMe', {}), ('getUpdates', {'limit': 100, 'timeout': 0})]
    assert 'private text' not in result.text and TELEGRAM_TOKEN not in result.text
    assert main.store.config() == before


def test_telegram_chat_discovery_no_messages_is_not_an_error(client, monkeypatch):
    def fake_post(self, url, **kwargs):
        return main.httpx.Response(200, json={'ok': True, 'result': [] if url.endswith('/getUpdates') else {'username': 'test_bot'}})
    monkeypatch.setattr(main.httpx.Client, 'post', fake_post)
    result = client.request('POST', '/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN})
    assert result.status_code == 200 and result.json() == {'bot_username': 'test_bot', 'chats': []}


@pytest.mark.parametrize('token', [None, '', 'bad', TELEGRAM_TOKEN + '/getUpdates'])
def test_telegram_chat_discovery_rejects_missing_or_invalid_token_without_network(client, monkeypatch, token):
    def unexpected(*args, **kwargs):
        pytest.fail('Invalid token must not trigger a Telegram request')
    monkeypatch.setattr(main.httpx.Client, 'post', unexpected)
    assert client.request('POST', '/api/telegram/chats', json={'telegram_token': token}).status_code == 400


def test_telegram_chat_discovery_conflict_does_not_remove_webhook(client, monkeypatch):
    calls = []
    def fake_post(self, url, **kwargs):
        method = url.rsplit('/', 1)[-1]; calls.append(method)
        return main.httpx.Response(200, json={'ok': True, 'result': {'username': 'test_bot'}}) if method == 'getMe' else main.httpx.Response(409, json={'ok': False, 'description': TELEGRAM_TOKEN})
    monkeypatch.setattr(main.httpx.Client, 'post', fake_post)
    result = client.request('POST', '/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN})
    assert result.status_code == 400 and '웹후크' in result.json()['detail']
    assert TELEGRAM_TOKEN not in result.text and calls == ['getMe', 'getUpdates']


@pytest.mark.parametrize('response', [
    main.httpx.Response(401, json={'ok': False, 'description': TELEGRAM_TOKEN}),
    main.httpx.Response(200, text=TELEGRAM_TOKEN),
    main.httpx.Response(200, json={'ok': True, 'result': None}),
])
def test_telegram_chat_discovery_errors_hide_token(client, monkeypatch, response):
    monkeypatch.setattr(main.httpx.Client, 'post', lambda *args, **kwargs: response)
    result = client.request('POST', '/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN})
    assert result.status_code == 400 and TELEGRAM_TOKEN not in result.text


def test_telegram_chat_discovery_network_error_hides_token(client, monkeypatch):
    def fail(*args, **kwargs):
        raise main.httpx.ConnectError(TELEGRAM_TOKEN)
    monkeypatch.setattr(main.httpx.Client, 'post', fail)
    result = client.request('POST', '/api/telegram/chats', json={'telegram_token': TELEGRAM_TOKEN})
    assert result.status_code == 400 and TELEGRAM_TOKEN not in result.text


@pytest.mark.parametrize('chat_id', ['@public_channel', '@public_group', '987654321'])
def test_telegram_receiver_is_preserved_for_delivery(client, monkeypatch, chat_id):
    seen = []
    def fake_post(self, url, **kwargs):
        seen.append(kwargs['json']['chat_id'])
        return main.httpx.Response(200, json={'ok': True, 'result': {}})
    monkeypatch.setattr(main.httpx.Client, 'post', fake_post)
    assert client.request('POST', '/api/telegram/test', json={'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': chat_id}).status_code == 200
    assert seen == [chat_id]


def test_telegram_wrong_bot_username_explains_how_to_find_receiving_chat(client, monkeypatch):
    monkeypatch.setattr(main.httpx.Client, 'post', lambda *args, **kwargs: main.httpx.Response(400, json={'ok': False}))
    result = client.request('POST', '/api/telegram/test', json={'telegram_token': TELEGRAM_TOKEN, 'telegram_chat_id': '@sender_bot'})
    assert result.status_code == 400
    assert '/start' in result.text and '채팅 ID 찾기' in result.text and '봇의 @아이디' in result.text


@pytest.mark.parametrize('chat_id', ['-1001234567890', '@public_channel', ''])
def test_telegram_command_settings_require_private_chat(client, chat_id):
    result = client.put('/api/settings', json={'username': 'admin', 'telegram_token': TELEGRAM_TOKEN,
        'telegram_chat_id': chat_id, 'telegram_commands': True})
    assert result.status_code == 400 and not main.store.config()['telegram_commands']


def test_telegram_command_setting_can_enable_and_clear_credentials(client, monkeypatch):
    monkeypatch.setattr(main.notifications, 'submit', lambda *args: None)
    assert client.put('/api/settings', json={'username': 'admin', 'telegram_token': TELEGRAM_TOKEN,
        'telegram_chat_id': '42', 'telegram_commands': True}).status_code == 200
    assert client.get('/api/settings').json()['telegram_commands'] is True
    assert client.put('/api/settings', json={'username': 'admin', 'telegram_token': '', 'telegram_chat_id': ''}).status_code == 200
    assert client.get('/api/settings').json()['telegram_commands'] is False


def test_telegram_setup_notice_only_on_command_configuration_changes(client, monkeypatch):
    sent = []
    monkeypatch.setattr(main.notifications, 'submit', lambda fn, *args: fn(*args))
    monkeypatch.setattr(main, 'send_telegram', lambda *args: sent.append(args))

    def save(**values):
        assert client.put('/api/settings', json={'username': 'admin', **values}).status_code == 200

    save(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='42', telegram_commands=False)
    assert not sent
    save(telegram_commands=True)
    assert len(sent) == 1 and sent[-1][:2] == (TELEGRAM_TOKEN, '42')
    assert '봇 명령 사용 준비' in sent[-1][2]
    save(telegram_commands=True)
    save(events=['complete'])
    save(password='new-password-123')
    assert len(sent) == 1
    save(telegram_chat_id='43')
    assert len(sent) == 2 and sent[-1][1] == '43'
    save(telegram_token=TELEGRAM_TOKEN + 'x')
    assert len(sent) == 3 and sent[-1][0] == TELEGRAM_TOKEN + 'x'
    save(telegram_commands=False)
    assert len(sent) == 3
    save(telegram_commands=True)
    assert len(sent) == 4


def test_telegram_setup_notice_failure_does_not_fail_saved_settings(client, monkeypatch):
    monkeypatch.setattr(main.notifications, 'submit', lambda fn, *args: fn(*args))
    def fail(*args):
        raise ValueError('network')
    monkeypatch.setattr(main, 'send_telegram', fail)
    assert client.put('/api/settings', json={'username': 'admin', 'telegram_token': TELEGRAM_TOKEN,
        'telegram_chat_id': '42', 'telegram_commands': True}).status_code == 200
    assert main.store.config()['telegram_commands'] == 1
    assert any('Telegram 설정 완료 알림 전송 실패' in row['message'] for row in main.store.logs())


def test_command_mode_chat_discovery_uses_polling_cache_without_another_consumer(client, monkeypatch):
    main.store.update(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='42', telegram_commands=1)
    monkeypatch.setattr(main.telegram_bot, 'context', main.telegram_identity(main.store.config()))
    monkeypatch.setattr(main.telegram_bot, 'username', 'test_bot')
    monkeypatch.setattr(main.telegram_bot, 'chats', {'42': {'id': '42', 'type': 'private', 'name': '나'}})
    def unexpected(*args, **kwargs): pytest.fail('Discovery must not poll while command consumer is active')
    monkeypatch.setattr(main.httpx.Client, 'post', unexpected)
    result = client.request('POST', '/api/telegram/chats', json={})
    assert result.status_code == 200 and result.json()['chats'][0]['id'] == '42'


def test_telegram_confirmed_conversation_renames_only_selected_folder(client, service):
    from app.telegram_bot import TelegramBot
    _, root = service
    folder = root / '봇_선택폴더'; folder.mkdir()
    source = folder / '광고@영화.mkv'; source.write_text('media')
    outside = root / '광고@대상외.mkv'; outside.touch()
    main.store.update(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='42', telegram_commands=1, events='')
    replies = []
    def request(token, method, payload):
        replies.append(payload)
        return {'message_id': len(replies)}
    bot = TelegramBot(main.store.config, request, main.validate_telegram_job, main.start_telegram_job, main.store.log)
    bot.context = main.telegram_identity(main.store.config())
    for i, text in enumerate(['/change', '광고@', 'null', '/' + folder.name, '예'], 100):
        bot.handle({'message': {'message_id': i, 'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42}, 'text': text}})
        if text != '예': assert source.exists() and not (folder / '영화.mkv').exists()
    assert wait_job(client)['renamed'] == 1
    assert (folder / '영화.mkv').read_text() == 'media' and outside.exists()
    assert '작업을 시작했습니다' in replies[-1]['text']
    assert any('Telegram 대화에서 확인한 작업 시작' in row['message'] for row in main.store.logs())


def test_telegram_job_rechecks_identity_and_folder_at_confirmation(client, service):
    main.store.update(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='42', telegram_commands=1)
    expected = main.telegram_identity(main.store.config())
    main.store.update(telegram_commands=0)
    with pytest.raises(ValueError, match='설정이 변경'):
        main.start_telegram_job({'folder': '', 'find': 'ad_', 'replace': ''}, expected)
    main.store.update(telegram_commands=1)
    with pytest.raises(ValueError, match='폴더 경로'):
        main.start_telegram_job({'folder': '../outside', 'find': 'ad_', 'replace': ''}, expected)


@pytest.mark.parametrize('failed_channel', [None, 'discord', 'telegram'])
def test_both_notification_channels_filter_events_and_isolate_failures(client, monkeypatch, failed_channel):
    main.store.update(webhook='https://discord.com/api/webhooks/123/token',
                      telegram_token=TELEGRAM_TOKEN, telegram_chat_id='123456789', events='error')
    seen = []
    def deliver(channel, message):
        seen.append((channel, message))
        if channel == failed_channel:
            raise ValueError(TELEGRAM_TOKEN)
    monkeypatch.setattr(main, 'send_discord', lambda url, message: deliver('discord', message))
    monkeypatch.setattr(main, 'send_telegram', lambda token, chat_id, message: deliver('telegram', message))
    main.notify('start', 'skip'); main.notify('complete', 'skip'); main.notify('error', '⚠️ 한글 <파일>')
    main.notifications.submit(lambda: None).result(timeout=3)
    assert seen == [('discord', '⚠️ 한글 <파일>'), ('telegram', '⚠️ 한글 <파일>')]
    logs = client.get('/api/logs').json()
    assert TELEGRAM_TOKEN not in str(logs)
    if failed_channel:
        assert any(f'{failed_channel.title()} 알림 전송 실패' in row['message'] for row in logs)


def test_job_telegram_only_uses_same_start_and_result_messages(client, service, monkeypatch):
    _, root = service
    folder = root / 'telegram-job'; folder.mkdir(); (folder / 'ad_영화.mp4').touch()
    main.store.update(telegram_token=TELEGRAM_TOKEN, telegram_chat_id='123456789')
    seen = []
    monkeypatch.setattr(main, 'send_telegram', lambda token, chat_id, message: seen.append(message))
    assert client.post('/api/jobs', json={'folder': folder.name, 'find': 'ad_'}).status_code == 200
    assert wait_job(client)['renamed'] == 1
    main.notifications.submit(lambda: None).result(timeout=3)
    assert len(seen) == 2 and '작업 시작' in seen[0] and '작업 완료' in seen[1]
    assert (folder / '영화.mp4').exists()


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
