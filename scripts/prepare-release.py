#!/usr/bin/env python3
"""Export only reviewed public files. Does not read runtime data or Git credentials."""
import argparse
import hashlib
from pathlib import Path
import tarfile

PUBLIC_FILES = (
    '.gitignore', '.dockerignore', '.env.example', 'Dockerfile', 'compose.yaml', 'compose.build.yaml',
    'requirements.txt', 'requirements-dev.txt', 'README.md',
    '.github/workflows/ci.yml', '.github/workflows/publish.yml',
    'app/__init__.py', 'app/main.py', 'app/files.py', 'app/store.py', 'app/state.py', 'app/manage.py', 'app/telegram_bot.py',
    'app/static/index.html', 'app/static/app.js', 'app/static/style.css',
    'tests/test_service.py', 'tests/test_manage.py', 'tests/test_release.py', 'tests/test_telegram_bot.py', 'scripts/prepare-release.py',
)


def export(source, destination):
    source, destination = Path(source), Path(destination)
    names = list(PUBLIC_FILES)
    if (source / 'LICENSE').is_file():
        names.append('LICENSE')
    # Validate every input before creating output; reject symlinks to private files.
    for name in names:
        path = source / name
        if not path.is_file() or path.is_symlink() or path.resolve() != source.resolve() / name:
            raise ValueError(f'공개 파일 누락 또는 심볼릭 링크: {name}')
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    archive = destination.parent / (destination.name + '.tar.gz')
    if archive.exists():
        raise FileExistsError(archive)
    with tarfile.open(archive, 'w:gz') as out:
        for name in names:
            out.add(destination / name, arcname='svr-file-renamer/' + name, recursive=False)
    print(f'공개 소스: {destination}')
    print(f'소스 압축: {archive}')
    print('SHA256: ' + hashlib.sha256(archive.read_bytes()).hexdigest())
    return destination, archive


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='release/public', help='release/ 아래의 새 폴더')
    args = parser.parse_args()
    destination = (root / args.output).resolve()
    if not destination.is_relative_to(root / 'release') or destination == root / 'release':
        parser.error('출력은 release/ 아래 새 폴더로 지정하세요.')
    export(root, destination)


if __name__ == '__main__':
    main()
