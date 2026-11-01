"""Linux-only confined traversal and atomic, non-overwriting rename."""
import ctypes
import errno
import os
import re
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath

libc = ctypes.CDLL(None, use_errno=True)
libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
libc.renameat2.restype = ctypes.c_int
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def parts(path):
    if '\x00' in path or '\\' in path or any(p == '..' for p in path.split('/')):
        raise ValueError('상위 경로 또는 잘못된 경로는 사용할 수 없습니다.')
    return [p for p in path.split('/') if p and p != '.']


class Volume:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.fd = os.open(self.root, FLAGS)

    @contextmanager
    def directory(self, path=''):
        fd = os.dup(self.fd)
        try:
            for part in parts(path):
                next_fd = os.open(part, FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def folders(self, path):
        with self.directory(path) as fd:
            with os.scandir(fd) as entries:
                return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))

    def walk(self, path, stop, on_error):
        pending = [('/'.join(parts(path)))]
        while pending and not stop.is_set():
            current = pending.pop()
            try:
                with self.directory(current) as fd:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if stop.is_set():
                                return
                            relative = '/'.join(filter(None, [current, entry.name]))
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    pending.append(relative)
                                elif entry.is_file(follow_symlinks=False):
                                    info = entry.stat(follow_symlinks=False)
                                    yield relative, (info.st_dev, info.st_ino), info.st_size
                            except OSError as exc:
                                on_error(relative, exc)
            except OSError as exc:
                on_error(current or '/', exc)

    def rename(self, relative, replacement, identity):
        path = PurePosixPath(relative)
        if replacement in ('', '.', '..') or '/' in replacement or '\\' in replacement or '\x00' in replacement:
            raise ValueError('변경 결과가 비어 있거나 파일명으로 사용할 수 없습니다.')
        with self.directory(str(path.parent)) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                raise ValueError('탐색 이후 파일이 변경되었습니다.')
            if path.name == replacement:
                return replacement
            suffix = PurePosixPath(replacement).suffix
            stem = replacement[:-len(suffix)] if suffix else replacement
            # Strip an existing numeric suffix only when resolving a collision.
            base = re.sub(r'_\d+$', '', stem)
            candidate, n = replacement, 0
            while True:
                result = libc.renameat2(fd, os.fsencode(path.name), fd, os.fsencode(candidate), 1)
                if result == 0:
                    return candidate
                code = ctypes.get_errno()
                if code != errno.EEXIST:
                    raise OSError(code, os.strerror(code))
                n += 1
                candidate = f'{base}_{n}{suffix}'
