import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from pathlib import Path


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f'{salt}:{digest}'


def password_matches(password, hashed):
    return hmac.compare_digest(password_hash(password, hashed.split(':')[0]), hashed)


class Store:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(Path(folder) / 'app.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, username TEXT, password TEXT,
            must_change INTEGER, totp TEXT, last_step INTEGER, webhook TEXT, events TEXT);
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, time TEXT DEFAULT
            (strftime('%Y-%m-%dT%H:%M:%SZ','now')), folder TEXT, state TEXT);
          CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), job_id TEXT,
            path TEXT, old_name TEXT, new_name TEXT, size_bytes INTEGER);
          CREATE INDEX IF NOT EXISTS changes_job ON changes(job_id,id);
          CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT DEFAULT
            (strftime('%Y-%m-%dT%H:%M:%SZ','now')), level TEXT, message TEXT);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(config)')}
        for name in ('telegram_token', 'telegram_chat_id'):
            if name not in columns:
                self.db.execute(f"ALTER TABLE config ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        if not self.db.execute('SELECT id FROM config').fetchone():
            self.db.execute('INSERT INTO config (id,username,password,must_change,totp,last_step,webhook,events) VALUES (1,?,?,1,\'\',-1,\'\',?)',
                            ('admin', password_hash('admin'), 'start,complete,error'))
        self.db.commit()

    def config(self):
        with self.lock:
            return dict(self.db.execute('SELECT * FROM config WHERE id=1').fetchone())

    def update(self, **values):
        with self.lock:
            self.db.execute('UPDATE config SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=1', list(values.values()))
            self.db.commit()

    def log(self, level, message):
        with self.lock:
            self.db.execute('INSERT INTO logs(level,message) VALUES (?,?)', (level, message))
            self.db.execute('DELETE FROM logs WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM logs)')
            self.db.commit()

    def logs(self, before=0):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                'SELECT * FROM logs WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT 200', (before, before))]

    def reset(self, all_settings=False):
        """Use only under the offline state lock; update credentials atomically."""
        hashed = password_hash('admin')
        with self.lock, self.db:
            self.db.execute("UPDATE config SET username='admin', password=?, must_change=1, totp='', last_step=-1 WHERE id=1", (hashed,))
            if all_settings:
                self.db.execute("UPDATE config SET webhook='', telegram_token='', telegram_chat_id='', events='start,complete,error' WHERE id=1")
                self.db.execute('DELETE FROM logs')
                self.db.execute('DELETE FROM changes')
                self.db.execute('DELETE FROM jobs')
            message = '전체 설정 초기화' if all_settings else '관리자 계정 및 TOTP 초기화'
            self.db.execute("INSERT INTO logs(level,message) VALUES ('info',?)", (message,))

    def begin_job(self, job_id, folder):
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs(id,folder,state) VALUES (?,?,'running')", (job_id, folder))
            self.db.execute('DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY rowid DESC LIMIT 100)')
            self.db.execute('DELETE FROM changes WHERE job_id NOT IN (SELECT id FROM jobs)')

    def finish_job(self, job_id, state):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET state=? WHERE id=?', (state, job_id))

    def interrupt_jobs(self):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='interrupted' WHERE state='running'")

    def record_change(self, job_id, path, old_name, new_name, size_bytes):
        with self.lock, self.db:
            self.db.execute('INSERT INTO changes(job_id,path,old_name,new_name,size_bytes) VALUES (?,?,?,?,?)',
                            (job_id, path, old_name, new_name, size_bytes))
            self.db.execute('DELETE FROM changes WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM changes)')

    def changes(self, before=0, all_jobs=False):
        with self.lock:
            latest = self.db.execute('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()
            latest = dict(latest) if latest else None
            job_id = None if all_jobs else (latest['id'] if latest else '')
            rows = self.db.execute('SELECT * FROM changes WHERE (? IS NULL OR job_id=?) AND (?=0 OR id<?) ORDER BY id DESC LIMIT 200',
                                   (job_id, job_id, before, before)).fetchall()
            count = self.db.execute('SELECT COUNT(*) FROM changes WHERE (? IS NULL OR job_id=?)', (job_id, job_id)).fetchone()[0]
            return {'job': latest, 'total': count, 'rows': [dict(row) for row in rows]}
