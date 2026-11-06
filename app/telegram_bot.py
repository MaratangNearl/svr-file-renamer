"""Private-chat rename conversations and a single Telegram update consumer."""
import re
import secrets
import threading
import time


HELP = ('SVR FILE RENAMER\n/change: 파일명 변경 대화 시작\n'
        '찾을 문구 → 대체 문구(null이면 삭제) → 대상 폴더 → 작업 요약 → 예/아니오\n'
        '폴더는 연결된 미디어의 / 기준이며 하위 폴더도 포함합니다.\n'
        '/cancel: 입력 중인 작업 취소\n/help: 사용법\n'
        '확인은 10분 안에 완료하세요. 확인 전에는 파일을 변경하지 않습니다.')


def identity(config):
    if (config.get('must_change') or not config.get('telegram_commands')
            or not config.get('telegram_token')
            or not re.fullmatch(r'[1-9][0-9]*', config.get('telegram_chat_id', ''))):
        return None
    return (config['telegram_token'], config['telegram_chat_id'], config['username'], config['password'])


class TelegramBot:
    def __init__(self, config, request, validate, start_job, log):
        self.config, self.request, self.validate = config, request, validate
        self.start_job, self.log = start_job, log
        self.stopping = threading.Event()
        self.thread = None
        self.context = None
        self.offset = 0
        self.pending = None
        self.username = ''
        self.cache_lock = threading.RLock()
        self.chats = {}

    def start(self):
        self.thread = threading.Thread(target=self.run, name='telegram-commands', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join()

    def run(self):
        failed = False
        while not self.stopping.is_set():
            try:
                self.poll_once()
                failed = False
                delay = 1
            except Exception:
                if not failed:
                    self.log('error', 'Telegram 명령 수신 실패 (토큰, 전용 봇의 웹후크·중복 실행 또는 네트워크를 확인하세요.)')
                failed = True
                delay = 15
            self.stopping.wait(delay)

    def discovery(self, token):
        config = self.config()
        if identity(config) and config['telegram_token'] == token:
            with self.cache_lock:
                chats = list(self.chats.values()) if self.context == identity(config) else []
                return {'bot_username': self.username, 'chats': chats}
        return None

    def remember(self, update):
        for key in ('message', 'channel_post', 'my_chat_member'):
            chat = update.get(key, {}).get('chat', {})
            if chat.get('type') not in {'private', 'group', 'supergroup', 'channel'} or type(chat.get('id')) is not int:
                continue
            name = chat.get('title') or ' '.join(filter(None, (chat.get('first_name'), chat.get('last_name'))))
            with self.cache_lock:
                self.chats[str(chat['id'])] = {'id': str(chat['id']), 'type': chat['type'],
                                             'name': name or chat.get('username') or str(chat['id'])}
                if len(self.chats) > 100:
                    self.chats.pop(next(iter(self.chats)))

    def poll_once(self):
        context = identity(self.config())
        if context != self.context:
            self.pending = None
            with self.cache_lock:
                self.chats.clear()
                self.username = ''
            self.context = None
            self.offset = 0
        if context is None or self.stopping.is_set():
            return
        if self.context is None:
            bot = self.request(context[0], 'getMe', {})
            # Discard queued commands on enable/restart; never execute an old confirmation.
            updates = self.request(context[0], 'getUpdates', {'offset': -1, 'limit': 1, 'timeout': 0,
                                                            'allowed_updates': ['message', 'callback_query']})
            if identity(self.config()) != context or self.stopping.is_set():
                return
            with self.cache_lock:
                self.context = context
                self.username = bot['username']
            for update in updates:
                self.remember(update)
                self.offset = max(self.offset, update['update_id'] + 1)
            return
        updates = self.request(context[0], 'getUpdates', {'offset': self.offset, 'limit': 100, 'timeout': 5})
        for update in updates:
            if self.stopping.is_set() or identity(self.config()) != context:
                return
            if update['update_id'] < self.offset:
                continue
            # Advance before handling: network failures must not replay a confirmed job.
            self.offset = update['update_id'] + 1
            self.remember(update)
            self.handle(update)

    def send(self, text, keyboard=None):
        payload = {'chat_id': self.context[1], 'text': text[:4096],
                   'link_preview_options': {'is_disabled': True}}
        if keyboard:
            payload['reply_markup'] = {'inline_keyboard': [keyboard]}
        return self.request(self.context[0], 'sendMessage', payload)

    def handle(self, update):
        if self.stopping.is_set() or self.context is None or identity(self.config()) != self.context:
            return
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        sender = callback.get('from', {}) if callback else message.get('from', {})
        chat = message.get('chat', {})
        if (chat.get('type') != 'private' or str(chat.get('id')) != self.context[1]
                or str(sender.get('id')) != self.context[1] or sender.get('is_bot')):
            return
        text = message.get('text')
        if callback:
            try:
                self.request(self.context[0], 'answerCallbackQuery', {'callback_query_id': callback['id']})
            except Exception:
                pass
            data = callback.get('data', '')
            pending = self.pending
            if (not pending or pending['stage'] != 'confirm' or time.monotonic() > pending['expires']
                    or message.get('message_id') != pending.get('summary_id')
                    or data not in (f"change:{pending['nonce']}:yes", f"change:{pending['nonce']}:no")):
                self.send('만료되었거나 이미 처리한 확인입니다. /change로 다시 시작하세요.')
                return
            self.confirm(data.endswith(':yes'))
            return
        if not isinstance(text, str):
            self.send('텍스트로 답해주세요. /help에서 사용법을 확인할 수 있습니다.')
            return
        command = text.strip().split('@', 1)[0]
        if command in ('/start', '/help'):
            self.send(HELP)
            return
        if command == '/cancel':
            self.pending = None
            self.send('입력 중인 작업을 취소했습니다. 파일은 변경하지 않았습니다.')
            return
        if command == '/change':
            self.pending = {'stage': 'find', 'expires': time.monotonic() + 600, 'nonce': secrets.token_hex(8)}
            self.send('파일명에서 찾을 문구(변경 전 문구)를 입력하세요. /cancel로 취소할 수 있습니다.')
            return
        pending = self.pending
        if not pending or time.monotonic() > pending['expires']:
            self.pending = None
            self.send('진행 중인 입력이 없거나 10분이 지났습니다. /change로 시작하세요.')
            return
        stage = pending['stage']
        if stage == 'confirm':
            if text.strip() not in ('예', '아니오'):
                self.send('예 또는 아니오로 답하거나 아래 확인 버튼을 눌러주세요.')
            elif message.get('message_id', 0) <= pending.get('summary_id', 0):
                self.send('작업 요약을 확인한 뒤 예 또는 아니오를 새로 입력하세요.')
            else:
                self.confirm(text.strip() == '예')
            return
        if stage in ('find', 'replace'):
            value = '' if stage == 'replace' and text.strip().lower() == 'null' else text.strip('\u200b\ufeff\u2060')
            if len(value) > 255 or any(c in value for c in ('/', '\\', '\x00')) or (stage == 'find' and not value):
                self.send('1~255자의 찾을 문구 또는 255자 이하의 대체 문구를 입력하세요. 경로 구분자는 사용할 수 없습니다.')
                return
            if stage == 'replace' and value == pending['find']:
                self.send('찾을 문구와 대체 문구가 같습니다. 다른 대체 문구를 입력하세요. 삭제는 null입니다.')
                return
            pending[stage] = value
            pending['stage'] = 'replace' if stage == 'find' else 'folder'
            self.send('대체할 문구(변경 후 문구)를 입력하세요. 찾은 문구를 삭제하려면 null을 입력하세요.'
                      if stage == 'find' else '대상 폴더 경로를 입력하세요. 연결된 미디어의 루트는 / 입니다. 예: /영화\n하위 폴더도 포함합니다.')
            return
        try:
            values = self.validate({'find': pending['find'], 'replace': pending['replace'], 'folder': text})
        except ValueError as exc:
            self.send(f'{exc}\n대상 폴더를 다시 입력하세요. /cancel로 취소할 수 있습니다.')
            return
        replacement = repr(values['replace']) if values['replace'] else '삭제 (빈 문자열)'
        summary = (f"작업 내용\n대상 폴더: /{values['folder']}\n찾을 문구: {values['find']!r}\n"
                   f"대체 문구: {replacement}\n하위 폴더를 포함한 파일명만 변경합니다.\n이 작업을 수행할까요? 예 / 아니오")
        if len(summary) > 4096:
            self.send('작업 요약이 너무 깁니다. 더 짧은 폴더 경로를 입력하세요.')
            return
        result = self.send(summary, [{'text': '예', 'callback_data': f"change:{pending['nonce']}:yes"},
                                     {'text': '아니오', 'callback_data': f"change:{pending['nonce']}:no"}])
        pending.update(stage='confirm', values=values, summary_id=result['message_id'])

    def confirm(self, accepted):
        pending, self.pending = self.pending, None
        if not accepted:
            self.send('작업을 취소했습니다. 파일은 변경하지 않았습니다.')
            return
        try:
            result = self.start_job(pending['values'], self.context)
        except ValueError as exc:
            self.send(f'작업을 시작하지 못했습니다: {exc}\n/change로 다시 시작하세요.')
            return
        self.send(f"작업을 시작했습니다. 작업 ID: {result['id']}\n진행 상황은 웹에서 확인할 수 있습니다. 작업 결과 알림은 저장된 알림 설정을 따릅니다.")
