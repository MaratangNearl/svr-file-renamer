# SVR FILE RENAMER

Ubuntu/NAS의 Docker에서 실행하는 파일명 일괄 변경 웹 서비스입니다. **만든놈 Svrforum yohane**

한글·일본어·한자가 포함된 파일명에서 광고 문구를 지우거나 원하는 문구로 바꿉니다. 파일 내용과 폴더 이름은 바꾸지 않습니다.

- 연결한 볼륨 안에서 하위 폴더까지 탐색, 심볼릭 링크 건너뛰기
- 중복 파일명은 `_1`, `_2`… 번호 부여, 덮어쓰기 방지
- 탐색 중 회전 표시, 변경 중 진행률·처리 수량, 대상이 없으면 ‘수행할 작업이 없습니다’
- 진행상황의 **변경내용 확인**에서 경로·변경 전후 이름·용량 조회
- 최초 계정 설정, 선택형 TOTP, Discord 알림과 연결 테스트

## 1. 설치 파일 다운로드

Docker Engine과 `docker compose` 플러그인이 설치된 Ubuntu에서 실행하세요. 소스 빌드나 Python 설치는 필요하지 않습니다. NAS 저장소를 수정할 권한이 있는 일반 사용자로 SSH 접속합니다.

> 제작자가 첫 정식 릴리스를 게시하고 GHCR 이미지를 Public으로 전환한 후 아래 다운로드가 가능합니다. 게시 전 로컬 시험은 [제작자 가이드](docs/MAINTAINER.md)를 이용하세요.

```bash
sudo install -d -o "$(id -u)" -g "$(id -g)" /opt/stacks/svr-file-renamer
cd /opt/stacks/svr-file-renamer
curl -fL https://github.com/MaratangNearl/svr-file-renamer/releases/latest/download/compose.yaml -o compose.yaml
curl -fL https://github.com/MaratangNearl/svr-file-renamer/releases/latest/download/svr-file-renamer.env.example -o .env
chmod 600 .env
```

재설치나 업데이트 시 기존 `.env`를 덮어쓰지 마세요. 설치 폴더가 기존 배포 경로라면 아래 업데이트 절차를 사용합니다.

## 2. 저장소와 계정 권한 설정

```bash
id
nano .env
```

예시:

```dotenv
IMAGE=ghcr.io/maratangnearl/svr-file-renamer:latest
MEDIA_PATH=/srv/media
CONFIG_PATH=./data
PUID=1000
PGID=1000
BIND_ADDRESS=0.0.0.0
PORT=18080
TZ=Asia/Seoul
COOKIE_SECURE=false
```

`MEDIA_PATH`는 **실제 파일 저장소의 호스트 절대 경로**, PUID/PGID는 `id -u`, `id -g` 결과로 변경하세요. 해당 UID/GID는 대상 폴더에 쓰기·접근 권한이 있어야 합니다. `CONFIG_PATH`는 계정·TOTP·웹후크·이력을 보관하는 별도 폴더이며 미디어 저장소 밖에 두세요.

기본 `CONFIG_PATH=./data`라면:

```bash
mkdir -p data
chmod 700 data
```

설정한 PUID/PGID가 현재 SSH 사용자와 다르면 `data` 소유자도 해당 UID/GID로 바꿉니다. 예: `sudo chown 1000:1000 data`. `MEDIA_PATH`는 미리 존재해야 하며, 잘못된 경로에 빈 저장소를 자동 생성하지 않습니다. `CONFIG_PATH`를 바꾼 경우에도 그 폴더를 미리 만들고 소유자를 설정하세요.

## 3. 이미지 다운로드와 실행

```bash
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Windows/다른 PC 브라우저에서 **http://NAS_IP:18080**으로 접속합니다. 예: `http://192.168.0.20:18080`. NAS IP는 `hostname -I`로 확인합니다. PuTTY 터널은 필요하지 않습니다. `0.0.0.0`은 바인드 설정이며 브라우저에 입력하는 주소가 아닙니다.

이미 `BIND_ADDRESS=127.0.0.1`로 실행 중이면 `.env`에서 `0.0.0.0`으로 바꾼 후 `sudo docker compose up -d --force-recreate`를 실행하세요. LAN 접속이 안 되면 PC와 NAS의 통신 및 방화벽의 TCP 18080 허용 여부를 확인합니다.

공개 이미지는 GitHub 로그인 없이 받을 수 있습니다. `denied`가 나오면 제작자가 GHCR 패키지 공개 여부와 이미지 주소를 확인해야 합니다. [GHCR 공개 이미지 안내](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

## 사용법

1. 최초 로그인은 **admin / admin**입니다. 아이디와 10자 이상의 새 비밀번호를 저장해야 사용할 수 있습니다. 초기 화면에는 TOTP/Discord 설정이 없습니다.
2. 대상 폴더를 선택합니다. 웹 UI의 `/`가 `MEDIA_PATH`이며 그 위로는 이동할 수 없습니다.
3. 찾을 문구와 대체할 문구를 입력합니다. 대체 입력이 비어 있으면 찾은 문구를 삭제합니다. 일반 문자열을 대소문자를 구분하여 모든 위치에서 치환합니다.
4. 작업을 시작합니다. 중단 시 이미 변경한 파일명은 유지됩니다.
5. 진행상황의 **변경내용 확인**에서 성공한 변경의 폴더 경로, 변경 전후 파일명, 파일 용량을 확인합니다. 최근 작업/전체 이력, 새로고침, 이전 내역 보기를 제공합니다.

| 찾을 문구 | 대체 문구 | 변경 전 | 변경 후 |
| --- | --- | --- | --- |
| `abcd.com@` | 비움 | `abcd.com@movie1.mp4` | `movie1.mp4` |
| `#addomain.cn` | 비움 | `anime-1#addomain.cn.mkv` | `anime-1.mkv` |
| `anime-` | `anime#` | `anime-1.mkv` | `anime#1.mkv` |

`movie1.mp4`와 `movie1_1.mp4`가 이미 있으면 다음 변경 파일은 `movie1_2.mp4`입니다. 충돌 결과에 다시 `_1`을 중첩하지 않습니다. 빈 이름이나 `.`/`..` 등 잘못된 결과는 원본을 유지하고 오류로 기록합니다. 입력 앞뒤의 보이지 않는 복사 문자(U+200B, U+FEFF, U+2060)는 정리하며 일반 공백과 내부 문자는 유지합니다.

변경 이력은 최근 100개 작업 내 최대 10,000건을 보관하며 재시작 후에도 조회할 수 있습니다. 용량은 탐색 시점의 바이트 수입니다. 성공한 변경만 이력에 표시하며 개별 오류는 관리자 로그에서 확인합니다. 과거 버전의 일반 로그는 구조화된 이력으로 소급 변환하지 않습니다. 시작한 작업의 메모리 목록은 재시작 후 재개하지 않습니다.

## 관리자 설정

로그인 후 계정·Discord 설정을 저장할 때 비밀번호/TOTP를 다시 입력하지 않습니다. 저장하거나 창 밖을 클릭하면 설정 창이 닫힙니다. 계정 변경 시 다른 세션은 해제되고, 세션은 12시간 후 또는 서버 재시작 시 해제됩니다.

**TOTP는 선택 사항**입니다. 키 복사 또는 QR 스캔 후 인증 앱의 6자리 코드로 등록을 확인합니다. 활성화 후 로그인할 때 코드를 입력합니다. 사용한 코드는 재사용할 수 없으므로 등록 직후 로그인할 때는 다음 코드를 기다리세요. 관리자 설정 저장·TOTP 해제 시 재입력은 요구하지 않습니다. QR은 서버에서 생성합니다. 등록 대기는 10분 후 만료되며 복구 코드는 별도 제공하지 않습니다.

Discord 웹후크를 입력하고 연결 테스트를 실행한 뒤 설정을 저장합니다. 테스트는 주소를 자동 저장하지 않습니다. `작업 시작`, `작업 결과`(완료·중단·작업 없음), `에러` 알림을 각각 선택합니다. 작업 내 첫 오류만 Discord에 보내고 개별 오류는 모두 로그에 남깁니다. 전송 실패는 파일 작업을 중단하지 않습니다. 웹후크 입력을 비우면 저장된 주소를 유지하며 삭제 체크박스로 지울 수 있습니다. 일반 로그는 최근 10,000건을 UTC 시간으로 보관합니다.

## 업데이트와 중지

일반 업데이트는 **계정·비밀번호·TOTP·이력을 유지**합니다. 새 버전의 추가 테이블은 시작 시 생성합니다. `data`는 업데이트 전에 서비스를 잠시 중지하고 백업할 수 있습니다.

```bash
cd /opt/stacks/svr-file-renamer
sudo docker compose pull
sudo docker compose up -d
```

특정 버전으로 고정하려면 `.env`의 IMAGE 끝을 `:latest` 대신 게시된 버전(예: `:v1.0.0`)으로 바꿉니다. 새 버전이 바뀔 때만 이미지를 pull하고 컨테이너를 재생성합니다.

```bash
sudo docker compose logs --tail=100
sudo docker compose down
```

`down`과 `down -v`는 이 구성의 호스트 bind mount 데이터를 삭제하지 않습니다. 재시작은 `sudo docker compose up -d`입니다.

## 계정 복구와 초기화

계정/TOTP만 초기화하고 Discord와 이력을 유지하려면:

```bash
(
  set -eu
  sudo docker compose stop adname
  sudo docker compose run --rm --no-deps adname python -m app.manage reset-admin --yes
  sudo docker compose up -d adname
)
```

`reset-admin`을 `reset-all`로 바꾸면 계정·TOTP·Discord·일반 로그·변경 이력도 초기화합니다. 초기화 기록 1건은 새로 남깁니다. 모든 초기화 명령은 미디어 파일을 삭제하거나 이름을 되돌리지 않습니다. 실행 중인 서비스의 설정은 잠금으로 보호하므로 먼저 중지해야 합니다. 초기화 후 `admin/admin`으로 로그인하면 초기 계정 설정 화면이 표시됩니다.

## 운영 범위

- Ubuntu/Linux의 `renameat2(RENAME_NOREPLACE)`로 덮어쓰기를 방지합니다. 이 기능을 지원하지 않는 파일시스템에서는 오류로 처리합니다. NAS/CIFS 마운트의 지원과 권한을 확인하세요.
- 호스트의 다른 프로그램이 작업 중 대상 폴더를 옮기거나 같은 파일을 변경하는 일은 피하세요. 원자적 롤백은 제공하지 않습니다.
- 대상 볼륨 안에 추가로 마운트한 파일시스템도 탐색 대상입니다. 심볼릭 링크는 따라가지 않습니다.
- 설정 폴더에는 TOTP 키·웹후크가 저장되므로 접근 권한을 제한하세요. 설정을 미디어 저장소 안에 두지 마세요.
- 한 번에 작업 하나, Uvicorn worker 하나로 실행합니다. 변경 대상 목록 크기에 비례해 메모리를 사용합니다.
- 기본 HTTP는 내부망 접속용입니다. 외부 공개 시 HTTPS 리버스 프록시를 사용하고 `COOKIE_SECURE=true`로 설정하세요. 프록시는 원본 Host를 유지해야 합니다.

제작자·기여자를 위한 소스 실행, 테스트, GitHub 업로드와 이미지 릴리스 절차는 [제작자 가이드](docs/MAINTAINER.md)를 참고하세요.
