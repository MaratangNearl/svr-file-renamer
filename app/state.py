"""One owner of persistent state: the service or an offline maintenance command."""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class StateBusy(RuntimeError):
    pass


@contextmanager
def state_lock(folder):
    Path(folder).mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fd = os.open(Path(folder) / '.instance.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateBusy('서비스가 실행 중입니다. docker compose stop adname 후 초기화하세요.') from exc
        yield
    finally:
        os.close(fd)
