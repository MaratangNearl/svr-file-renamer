import base64
import io
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pyotp
import qrcode
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .files import Volume, parts
from .state import state_lock
from .store import Store, password_hash, password_matches

DATA = os.environ.get('APP_DATA', '/config')
ROOT = os.environ.get('MEDIA_ROOT', '/media')
store = None
volume = None
sessions = {}
failures = {}
auth_lock = threading.RLock()
job_lock = threading.RLock()
stop = threading.Event()
executor = ThreadPoolExecutor(max_workers=1)
notifications = ThreadPoolExecutor(max_workers=1)
job = {'state': 'idle', 'percent': 0, 'scanned': 0, 'processed': 0,
       'total': 0, 'renamed': 0, 'errors': 0, 'current': '', 'id': ''}
ACTIVE = {'scanning', 'running', 'stopping'}


@asynccontextmanager
async def lifespan(app):
    global store, volume
    with state_lock(DATA):
        store = Store(DATA)
        store.interrupt_jobs()
        volume = Volume(ROOT)
        store.log('info', '서비스 시작')
        try:
            yield
        finally:
            stop.set()
            executor.shutdown(wait=True)
            notifications.shutdown(wait=True)
            os.close(volume.fd)
            store.db.close()


app = FastAPI(title='SVR FILE RENAMER', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def security(request: Request, call_next):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        if request.headers.get('x-requested-with') != 'adname-ui':
            return Response(status_code=403)
        origin = request.headers.get('origin')
        if origin and (urlsplit(origin).netloc != request.headers.get('host') or urlsplit(origin).scheme not in ('http', 'https')):
            return Response(status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


def session(request: Request):
    token = request.cookies.get('session', '')
    with auth_lock:
        result = sessions.get(token)
        if not result or result['expires'] < time.time():
            sessions.pop(token, None)
            raise HTTPException(401, '로그인이 필요합니다.')
        return result


def ready(current=Depends(session)):
    if store.config()['must_change']:
        raise HTTPException(403, '먼저 기본 비밀번호를 변경해 주세요.')
    return current


def check_otp(code, config):
    if not config['totp']:
        return True
    otp = pyotp.TOTP(config['totp'])
    for offset in (-1, 0, 1):
        step = int(time.time() // 30) + offset
        if step > config['last_step'] and secrets.compare_digest(otp.at(step * 30), code):
            store.update(last_step=step)
            return True
    return False


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    code: str = Field(default='', max_length=8)


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    key = request.client.host if request.client else 'unknown'
    with auth_lock:
        now = time.time()
        attempts = [t for t in failures.get(key, []) if now - t < 300]
        if len(attempts) >= 10:
            raise HTTPException(429, '로그인 시도가 많습니다. 5분 후 다시 시도하세요.')
        config = store.config()
        if (not secrets.compare_digest(body.username.encode(), config['username'].encode())
                or not password_matches(body.password, config['password'])
                or not check_otp(body.code, config)):
            failures[key] = attempts + [now]
            raise HTTPException(401, '아이디, 비밀번호 또는 인증 코드를 확인하세요.')
        failures.pop(key, None)
        for old in list(sessions):
            if sessions[old]['expires'] < now:
                sessions.pop(old)
        token = secrets.token_urlsafe(32)
        sessions[token] = {'expires': now + 43200, 'pending_totp': ''}
        response.set_cookie('session', token, httponly=True, samesite='strict', max_age=43200,
                            secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')
        store.log('info', '관리자 로그인')
        return {'must_change': bool(config['must_change'])}


@app.get('/api/me')
def me(current=Depends(session)):
    config = store.config()
    return {'username': config['username'], 'must_change': bool(config['must_change'])}


@app.post('/api/logout')
def logout(request: Request, response: Response, current=Depends(session)):
    with auth_lock:
        sessions.pop(request.cookies.get('session'), None)
    response.delete_cookie('session')
    return {'ok': True}


@app.get('/api/settings')
def settings(current=Depends(session)):
    config = store.config()
    return {'username': config['username'], 'must_change': bool(config['must_change']),
            'totp_enabled': bool(config['totp']), 'webhook_configured': bool(config['webhook']),
            'telegram_configured': bool(config['telegram_token'] and config['telegram_chat_id']),
            'events': config['events'].split(',') if config['events'] else []}


class Settings(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(default='', max_length=256)
    webhook: str | None = Field(default=None, max_length=500)
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    events: list[str] | None = None


def webhook_valid(url):
    return bool(re.fullmatch(r'https://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[\w-]+', url))


def validate_telegram(token, chat_id):
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,200}', token):
        raise ValueError('올바른 Telegram 봇 토큰을 입력하세요.')
    if not re.fullmatch(r'-?[1-9][0-9]*|@[A-Za-z][A-Za-z0-9_]{4,31}', chat_id):
        raise ValueError('올바른 Telegram 채팅 ID 또는 @채널이름을 입력하세요.')


@app.put('/api/settings')
def save_settings(body: Settings, request: Request, current=Depends(session)):
    with auth_lock:
        config = store.config()
        if config['must_change'] and any(value is not None for value in
                (body.webhook, body.events, body.telegram_token, body.telegram_chat_id)):
            raise HTTPException(403, '먼저 아이디와 비밀번호를 설정하세요.')
        if config['must_change'] and not body.password:
            raise HTTPException(400, '새 비밀번호를 입력하세요.')
        if body.password and (len(body.password) < 10 or body.password == 'admin'):
            raise HTTPException(400, '새 비밀번호는 10자 이상이어야 합니다.')
        if body.webhook and not webhook_valid(body.webhook):
            raise HTTPException(400, '올바른 Discord 웹후크 HTTPS 주소를 입력하세요.')
        if body.events is not None and set(body.events) - {'start', 'complete', 'error'}:
            raise HTTPException(400, '알림 종류가 올바르지 않습니다.')
        updates = {'username': body.username.strip()}
        if body.events is not None:
            updates['events'] = ','.join(body.events)
        if not updates['username']:
            raise HTTPException(400, '아이디를 입력하세요.')
        if body.webhook is not None:
            updates['webhook'] = body.webhook
        token = body.telegram_token if body.telegram_token is not None else config['telegram_token']
        chat_id = body.telegram_chat_id if body.telegram_chat_id is not None else config['telegram_chat_id']
        if token or chat_id:
            try:
                validate_telegram(token, chat_id)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        updates.update(telegram_token=token, telegram_chat_id=chat_id)
        if body.password:
            updates.update(password=password_hash(body.password), must_change=0)
        store.update(**updates)
        # Credential changes revoke every other session.
        if body.password or body.username != config['username']:
            for token in list(sessions):
                if token != request.cookies.get('session'):
                    sessions.pop(token)
        store.log('info', '관리자 설정 저장')
        return {'ok': True}


class InitialAccount(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=256)


@app.post('/api/account/setup')
def initial_account(body: InitialAccount, request: Request, current=Depends(session)):
    with auth_lock:
        if not store.config()['must_change']:
            raise HTTPException(409, '이미 초기 설정을 완료했습니다.')
        return save_settings(Settings(username=body.username, password=body.password), request, current)


@app.post('/api/totp/setup')
def totp_setup(current=Depends(ready)):
    with auth_lock:
        if store.config()['totp']:
            raise HTTPException(409, '이미 활성화되어 있습니다. 해제 후 다시 등록하세요.')
        secret = pyotp.random_base32()
        current['pending_totp'] = (secret, time.time() + 600)
        uri = pyotp.TOTP(secret).provisioning_uri(store.config()['username'], issuer_name='SVR FILE RENAMER')
        output = io.BytesIO()
        qrcode.make(uri).save(output, format='PNG')
        return {'key': secret, 'qr': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()}


class TotpAction(BaseModel):
    code: str = Field(max_length=8)


@app.post('/api/totp/enable')
def totp_enable(body: TotpAction, request: Request, current=Depends(ready)):
    with auth_lock:
        pending = current.get('pending_totp')
        if store.config()['totp'] or not pending or pending[1] < time.time():
            raise HTTPException(400, 'TOTP 등록을 다시 시작하세요.')
        secret = pending[0]
        if not pyotp.TOTP(secret).verify(body.code):
            raise HTTPException(400, '인증 코드가 일치하지 않습니다.')
        store.update(totp=secret, last_step=int(time.time() // 30))
        current['pending_totp'] = ''
        for token in list(sessions):
            if token != request.cookies.get('session'):
                sessions.pop(token)
        store.log('info', 'TOTP 활성화')
        return {'ok': True}


@app.post('/api/totp/disable')
def totp_disable(current=Depends(ready)):
    with auth_lock:
        store.update(totp='', last_step=-1)
        store.log('info', 'TOTP 비활성화')
        return {'ok': True}


def send_discord(url, message):
    if not webhook_valid(url):
        raise ValueError('Discord 웹후크를 먼저 설정하세요.')
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        result = client.post(url, json={'content': message[:1900], 'allowed_mentions': {'parse': []}})
        if result.status_code not in (200, 204):
            raise ValueError(f'Discord 응답 오류: HTTP {result.status_code}')


def send_telegram(token, chat_id, message):
    validate_telegram(token, chat_id)
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
        result = client.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': message[:1900],
                                   'link_preview_options': {'is_disabled': True}})
        if result.status_code != 200:
            raise ValueError(f'Telegram 응답 오류: HTTP {result.status_code}')
        try:
            successful = result.json().get('ok') is True
        except (ValueError, AttributeError):
            successful = False
        if not successful:
            raise ValueError('Telegram 전송 실패: 봇 토큰, 채팅 ID와 메시지 전송 권한을 확인하세요.')


def notify(event, message):
    config = store.config()
    if event not in config['events'].split(','):
        return

    def deliver():
        if config['webhook']:
            try:
                send_discord(config['webhook'], message)
            except Exception:
                store.log('error', 'Discord 알림 전송 실패 (주소, 권한 또는 네트워크를 확인하세요.)')
        if config['telegram_token'] and config['telegram_chat_id']:
            try:
                send_telegram(config['telegram_token'], config['telegram_chat_id'], message)
            except Exception:
                store.log('error', 'Telegram 알림 전송 실패 (봇 토큰, 채팅 ID, 권한 또는 네트워크를 확인하세요.)')

    if config['webhook'] or (config['telegram_token'] and config['telegram_chat_id']):
        notifications.submit(deliver)


class WebhookTest(BaseModel):
    webhook: str | None = Field(default=None, max_length=500)


@app.post('/api/webhook/test')
def webhook_test(body: WebhookTest, current=Depends(ready)):
    try:
        send_discord(body.webhook if body.webhook is not None else store.config()['webhook'], '✅ SVR FILE RENAMER: Discord 연결 테스트 성공')
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else 'Discord 연결에 실패했습니다.') from exc
    return {'ok': True}


class TelegramTest(BaseModel):
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_chat_id: str | None = Field(default=None, max_length=64)


@app.post('/api/telegram/test')
def telegram_test(body: TelegramTest, current=Depends(ready)):
    config = store.config()
    try:
        send_telegram(body.telegram_token if body.telegram_token is not None else config['telegram_token'],
                      body.telegram_chat_id if body.telegram_chat_id is not None else config['telegram_chat_id'],
                      '✅ SVR FILE RENAMER: Telegram 연결 테스트 성공')
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else
                            'Telegram 연결에 실패했습니다. 봇 토큰, 채팅 ID, 권한 또는 네트워크를 확인하세요.') from exc
    return {'ok': True}


@app.get('/api/folders')
def folders(path: str = '', current=Depends(ready)):
    try:
        return {'path': '/'.join(parts(path)), 'folders': volume.folders(path)}
    except (ValueError, OSError) as exc:
        raise HTTPException(400, '접근할 수 없는 폴더입니다. 경로와 권한을 확인하세요.') from exc


@app.get('/api/changes')
def changes(before: int = 0, all_jobs: bool = False, current=Depends(ready)):
    return store.changes(before=before, all_jobs=all_jobs)


@app.get('/api/logs')
def logs(before: int = 0, current=Depends(ready)):
    return store.logs(before)


def update_job(**values):
    with job_lock:
        job.update(values)


def run_job(body, job_id):
    error_notified = False
    def error(path, exc):
        nonlocal error_notified
        with job_lock:
            job['errors'] += 1
        store.log('error', f'{path}: {exc}')
        if not error_notified:
            notify('error', f'⚠️ SVR FILE RENAMER 작업 중 오류 발생: {path}\n자세한 내용은 관리자 로그를 확인하세요.')
            error_notified = True
    try:
        store.log('info', f'작업 시작: /{body.folder}, 찾기={body.find!r}, 대체={body.replace!r}')
        notify('start', f'▶️ SVR FILE RENAMER 작업 시작: /{body.folder}')
        matches = []
        for relative, identity, size_bytes in volume.walk(body.folder, stop, error):
            with job_lock:
                job['scanned'] += 1
            if body.find in Path(relative).name:
                matches.append((relative, identity, size_bytes))
        update_job(total=len(matches))
        if not stop.is_set():
            update_job(state='running')
        # Sort for reproducible collision allocation.
        for relative, identity, size_bytes in sorted(matches):
            if stop.is_set():
                break
            update_job(current=relative)
            name = Path(relative).name
            target = name.replace(body.find, body.replace)
            try:
                result = volume.rename(relative, target, identity)
                if result != name:
                    with job_lock:
                        job['renamed'] += 1
                    parent = str(Path(relative).parent)
                    store.record_change(job_id, '/' if parent == '.' else '/' + parent, name, result, size_bytes)
                    store.log('rename', f'{relative} → {result}')
            except (OSError, ValueError) as exc:
                error(relative, exc)
            with job_lock:
                job['processed'] += 1
                job['percent'] = int(job['processed'] / max(job['total'], 1) * 100)
        with job_lock:
            if stop.is_set():
                state = 'stopped'
            elif job['total'] == 0 and job['errors'] == 0:
                state = 'no_work'
            else:
                state = 'completed'
            if state == 'completed':
                job['percent'] = 100
            if state == 'no_work':
                message = f'수행할 작업이 없습니다: /{body.folder} (탐색 {job["scanned"]}개 파일)'
            else:
                message = f'작업 {"중단" if state == "stopped" else "완료"}: 처리 {job["processed"]}/{job["total"]}, 변경 {job["renamed"]}, 오류 {job["errors"]}'
        store.log('info', message)
        notify('complete', f'SVR FILE RENAMER\n{message}')
        store.finish_job(job_id, state)
        update_job(state=state, current='')
    except Exception as exc:
        error('작업', exc)
        store.finish_job(job_id, 'error')
        update_job(state='error', current='')


class RenameJob(BaseModel):
    folder: str = Field(default='', max_length=4096)
    find: str = Field(min_length=1, max_length=255)
    replace: str = Field(default='', max_length=255)


@app.post('/api/jobs')
def start_job(body: RenameJob, current=Depends(ready)):
    # Strip copy/paste artifacts at the edges; preserve ordinary spaces and
    # meaningful Unicode inside the literal search/replacement strings.
    body.find = body.find.strip('\u200b\ufeff\u2060')
    body.replace = body.replace.strip('\u200b\ufeff\u2060')
    if not body.find:
        raise HTTPException(400, '찾을 문구를 입력하세요. 보이지 않는 문자만 사용할 수 없습니다.')
    if any(c in body.find + body.replace for c in ('/', '\\', '\x00')):
        raise HTTPException(400, '찾기·대체 문구에 경로 구분자는 사용할 수 없습니다.')
    if body.find == body.replace:
        raise HTTPException(400, '찾을 문구와 대체 문구가 같습니다.')
    try:
        body.folder = '/'.join(parts(body.folder))
        with volume.directory(body.folder):
            pass
    except (OSError, ValueError) as exc:
        raise HTTPException(400, '폴더 경로 또는 접근 권한을 확인하세요.') from exc
    with job_lock:
        if job['state'] in ACTIVE:
            raise HTTPException(409, '이미 실행 중인 작업이 있습니다.')
        stop.clear()
        job.update(state='scanning', percent=0, scanned=0, processed=0, total=0,
                   renamed=0, errors=0, current='', id=secrets.token_hex(8))
        store.begin_job(job['id'], body.folder)
        executor.submit(run_job, body, job['id'])
        return dict(job)


@app.get('/api/jobs')
def job_status(current=Depends(ready)):
    with job_lock:
        return dict(job)


@app.post('/api/jobs/stop')
def stop_job(current=Depends(ready)):
    with job_lock:
        if job['state'] in ACTIVE:
            stop.set()
            job['state'] = 'stopping'
        return dict(job)


STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/healthz')
def health():
    return {'ok': True}
