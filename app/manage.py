"""Offline administrator recovery. Never touches MEDIA_ROOT."""
import argparse
import os
import sys

from .state import StateBusy, state_lock
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description='SVR FILE RENAMER 설정 초기화 (서비스 중지 후 사용)')
    parser.add_argument('command', choices=['reset-admin', 'reset-all'])
    parser.add_argument('--yes', action='store_true', help='초기화를 명시적으로 승인')
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error('계정/TOTP를 초기화하려면 --yes가 필요합니다. reset-all은 Discord/Telegram 설정과 기존 로그도 초기화합니다.')
    try:
        with state_lock(os.environ.get('APP_DATA', '/config')):
            store = Store(os.environ.get('APP_DATA', '/config'))
            try:
                store.reset(all_settings=args.command == 'reset-all')
            finally:
                store.db.close()
    except (StateBusy, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('초기화 완료: 아이디 admin / 비밀번호 admin / TOTP 해제. 다음 로그인에서 새 계정을 설정하세요.')
    print('영상 파일과 파일명은 변경하지 않았습니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
