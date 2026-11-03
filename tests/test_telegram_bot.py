import time

import pytest

from app.telegram_bot import HELP, TelegramBot, identity


@pytest.fixture
def bot():
    config = {'must_change': 0, 'username': 'admin', 'password': 'hash',
              'telegram_commands': 1, 'telegram_token': '123:TEST_ONLY', 'telegram_chat_id': '42'}
    sent, jobs, requests = [], [], []
    counter = [100]
    def request(token, method, payload):
        requests.append((method, payload))
        if method == 'sendMessage':
            counter[0] += 1
            sent.append(payload)
            return {'message_id': counter[0]}
        if method == 'getMe':
            return {'username': 'test_bot'}
        return []
    def validate(values):
        if values['folder'] not in ('/', '/영화'):
            raise ValueError('폴더 경로 또는 접근 권한을 확인하세요.')
        return dict(values, folder=values['folder'].strip('/'))
    def start(values, context):
        jobs.append(dict(values))
        return {'id': 'job123'}
    instance = TelegramBot(lambda: config, request, validate, start, lambda *args: None)
    instance.context = identity(config)
    instance.test_config, instance.sent, instance.jobs, instance.requests = config, sent, jobs, requests
    return instance


def message(bot, text, sender=42, chat=42, kind='private', message_id=1000):
    bot.handle({'message': {'message_id': message_id, 'chat': {'id': chat, 'type': kind},
                           'from': {'id': sender}, 'text': text}})


def summary(bot, replace='null', folder='/영화'):
    for text in ['/change', '광고@', replace, folder]:
        message(bot, text)
    assert not bot.jobs
    assert bot.pending['stage'] == 'confirm'


def callback(bot, data=None, sender=42):
    nonce = bot.pending['nonce'] if bot.pending else 'old'
    summary_id = bot.pending['summary_id'] if bot.pending else 104
    bot.handle({'callback_query': {'id': 'callback1', 'from': {'id': sender},
                                 'message': {'message_id': summary_id, 'chat': {'id': 42, 'type': 'private'}},
                                 'data': data or f'change:{nonce}:yes'}})


@pytest.mark.parametrize('replace', ['null', ' NULL ', '새이름@'])
def test_change_asks_both_phrases_and_folder_before_explicit_yes(bot, replace):
    message(bot, '/change')
    assert '찾을 문구' in bot.sent[-1]['text'] and not bot.jobs
    message(bot, '광고@')
    assert '대체할 문구' in bot.sent[-1]['text'] and 'null' in bot.sent[-1]['text']
    message(bot, replace)
    assert '폴더 경로' in bot.sent[-1]['text'] and not bot.jobs
    message(bot, '/영화')
    summary_text = bot.sent[-1]['text']
    assert '대상 폴더: /영화' in summary_text and '광고@' in summary_text and '예 / 아니오' in summary_text
    assert '하위 폴더' in summary_text and not bot.jobs
    assert [b['text'] for b in bot.sent[-1]['reply_markup']['inline_keyboard'][0]] == ['예', '아니오']
    message(bot, '예')
    expected = '' if replace.strip().lower() == 'null' else replace
    assert bot.jobs == [{'find': '광고@', 'replace': expected, 'folder': '영화'}]
    assert bot.pending is None and '작업을 시작했습니다' in bot.sent[-1]['text']
    message(bot, '예')
    assert len(bot.jobs) == 1


def test_callback_confirmation_is_single_use(bot):
    summary(bot)
    data = f"change:{bot.pending['nonce']}:yes"
    callback(bot, data)
    assert len(bot.jobs) == 1
    callback(bot, data)
    assert len(bot.jobs) == 1
    assert any(method == 'answerCallbackQuery' for method, _ in bot.requests)


@pytest.mark.parametrize('answer', ['아니오', '/cancel'])
def test_cancel_never_changes_files(bot, answer):
    summary(bot)
    message(bot, answer)
    assert not bot.jobs and bot.pending is None and '취소' in bot.sent[-1]['text']


def test_no_button_cancels(bot):
    summary(bot)
    callback(bot, f"change:{bot.pending['nonce']}:no")
    assert not bot.jobs and bot.pending is None


@pytest.mark.parametrize(('sender', 'chat', 'kind'), [(99, 42, 'private'), (42, 99, 'private'), (42, 42, 'group')])
def test_other_users_and_group_chats_cannot_control_jobs(bot, sender, chat, kind):
    message(bot, '/change', sender, chat, kind)
    assert not bot.sent and bot.pending is None and not bot.jobs


def test_other_sender_cannot_confirm(bot):
    summary(bot)
    callback(bot, sender=99)
    assert not bot.jobs and bot.pending['stage'] == 'confirm'


def test_old_callback_cannot_confirm_new_request(bot):
    summary(bot)
    old_data = f"change:{bot.pending['nonce']}:yes"
    summary(bot, replace='새@')
    callback(bot, old_data)
    assert not bot.jobs
    callback(bot)
    assert len(bot.jobs) == 1 and bot.jobs[0]['replace'] == '새@'


def test_text_yes_must_arrive_after_summary(bot):
    summary(bot)
    message(bot, '예', message_id=bot.pending['summary_id'] - 1)
    assert not bot.jobs
    message(bot, '예')
    assert len(bot.jobs) == 1


def test_unknown_answer_does_not_confirm(bot):
    summary(bot)
    message(bot, 'maybe')
    assert not bot.jobs and bot.pending['stage'] == 'confirm'


def test_expired_confirmation_requires_new_change(bot):
    summary(bot)
    bot.pending['expires'] = time.monotonic() - 1
    callback(bot)
    assert not bot.jobs
    message(bot, '예')
    assert not bot.jobs and bot.pending is None


def test_account_change_or_disable_blocks_existing_confirmation(bot):
    summary(bot)
    bot.test_config['telegram_commands'] = 0
    message(bot, '예')
    assert not bot.jobs
    bot.test_config['telegram_commands'] = 1
    bot.test_config['password'] = 'changed'
    callback(bot)
    assert not bot.jobs


def test_invalid_folder_keeps_conversation_in_folder_stage(bot):
    for text in ['/change', '광고@', 'null', '../outside']:
        message(bot, text)
    assert not bot.jobs and bot.pending['stage'] == 'folder'
    assert '폴더' in bot.sent[-1]['text']
    message(bot, '/')
    message(bot, '예')
    assert bot.jobs[0]['folder'] == ''


@pytest.mark.parametrize('value', ['', '\u200b', 'bad/name', 'bad\\name', 'a' * 256])
def test_invalid_find_does_not_advance(bot, value):
    message(bot, '/change'); message(bot, value)
    assert bot.pending['stage'] == 'find' and not bot.jobs


def test_same_replacement_rejected_and_literal_spaces_preserved(bot):
    message(bot, '/change'); message(bot, ' a '); message(bot, ' a ')
    assert bot.pending['stage'] == 'replace'
    message(bot, ' b '); message(bot, '/'); message(bot, '예')
    assert bot.jobs[0]['find'] == ' a ' and bot.jobs[0]['replace'] == ' b '


def test_help_and_start_respond(bot):
    message(bot, '/help'); message(bot, '/start')
    assert [row['text'] for row in bot.sent] == [HELP, HELP]


def test_busy_job_failure_does_not_reuse_confirmation(bot):
    summary(bot)
    def busy(*args):
        raise ValueError('이미 실행 중인 작업이 있습니다.')
    bot.start_job = busy
    message(bot, '예')
    assert bot.pending is None and not bot.jobs and '이미 실행 중' in bot.sent[-1]['text']


def test_polling_bootstrap_discards_old_commands_and_advances_offset(bot):
    bot.context = None
    def request(token, method, payload):
        if method == 'getMe': return {'username': 'test_bot'}
        if method == 'getUpdates':
            assert payload['offset'] == -1
            return [{'update_id': 55, 'message': {'text': '/change', 'chat': {'id': 42, 'type': 'private'}}}]
        return {'message_id': 99}
    bot.request = request
    bot.poll_once()
    assert bot.offset == 56 and bot.pending is None and not bot.jobs
    assert bot.discovery('123:TEST_ONLY')['chats'][0]['id'] == '42'


def test_polling_deduplicates_updates_before_handling(bot):
    received = []
    update = {'update_id': 7, 'message': {'chat': {}}}
    bot.request = lambda *args: [update, update]
    bot.handle = lambda update: received.append(update['update_id'])
    bot.poll_once(); bot.poll_once()
    assert received == [7] and bot.offset == 8


def test_reply_failure_after_launch_never_repeats_job(bot):
    summary(bot)
    original = bot.request
    def fail(token, method, payload):
        if method == 'sendMessage': raise ValueError('network')
        return original(token, method, payload)
    bot.request = fail
    with pytest.raises(ValueError): message(bot, '예')
    assert len(bot.jobs) == 1 and bot.pending is None


def test_stopped_worker_cannot_start_job(bot):
    summary(bot); bot.stopping.set(); message(bot, '예')
    assert not bot.jobs


def test_full_conversation_through_update_polling(bot):
    bot.context = None
    original = bot.request
    queue = []
    def request(token, method, payload):
        if method == 'getUpdates':
            result, queue[:] = list(queue), []
            return result
        return original(token, method, payload)
    bot.request = request
    bot.poll_once()
    for update_id, text in enumerate(['/change', '광고@', 'null', '/영화', '예'], 10):
        queue.append({'update_id': update_id, 'message': {'message_id': update_id + 1000,
            'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42}, 'text': text}})
        bot.poll_once()
        if text != '예': assert not bot.jobs
    assert bot.jobs == [{'find': '광고@', 'replace': '', 'folder': '영화'}]
    assert bot.offset == 15


def test_worker_closes_without_waiting_for_poll_interval(bot):
    bot.test_config['telegram_commands'] = 0
    bot.start(); bot.close()
    assert not bot.thread.is_alive()
