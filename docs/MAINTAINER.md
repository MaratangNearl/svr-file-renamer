# 제작자 가이드 · SVR FILE RENAMER

제작자: **Svrforum yohane** · GitHub: **MaratangNearl/svr-file-renamer**

사용자 설치 문서는 [README](../README.md)입니다. 이 문서는 소스 개발, 공개 파일 검토, GitHub 게시와 이미지 릴리스를 설명합니다. 현재 작업 환경에 GitHub 인증이 연결되지 않았다면 실제 업로드와 첫 릴리스는 제작자가 진행해야 합니다.

## 공개할 파일과 로컬 전용 파일

`scripts/prepare-release.py`의 `PUBLIC_FILES`가 공개 파일 목록입니다. 소스, 자동 테스트, Compose, Dockerfile, 워크플로, 사용자·제작자 문서만 내보냅니다. 선택한 `LICENSE` 파일이 있으면 함께 포함합니다.

다음은 GitHub 및 소스 배포 패키지에 포함하지 않습니다.

- `PUTTY_TEST_GUIDE.md` 등 개인 테스트 안내
- `.env`, 실제 계정/TOTP/웹후크 DB, 로그, 미디어 파일, 백업
- `.venv`, Python 캐시, pytest 캐시, 로컬 Compose override
- `release/` 출력 폴더 자체

`.gitignore`도 이를 차단합니다. Docker 빌드 컨텍스트는 `.dockerignore`의 허용 목록으로 `app`, `requirements.txt`, Dockerfile만 전달합니다. 이미지에는 사용자 문서, 테스트 파일, 로컬 설정이 들어가지 않습니다. 이미 Git에 추적된 파일은 ignore로 제거되지 않으므로, 처음에는 아래의 **정리된 내보내기 폴더**에서 Git을 시작하세요.

## 로컬 개발과 검증

Linux, Python 3.12 이상, Docker Engine/Compose가 필요합니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

Node는 프런트엔드 문법 검사에만 쓰며 제품 실행에는 필요하지 않습니다. Python 테스트는 임시 저장소를 사용합니다. `.github/workflows/ci.yml`은 GitHub에서 테스트·Compose 검증·Docker 빌드·컨테이너 health check까지 실행합니다.

소스에서 컨테이너를 빌드하려면 기본 배포용 Compose 대신 `compose.build.yaml`을 명시합니다.

```bash
cp .env.example .env
nano .env
mkdir -p data
chmod 700 data
docker compose -f compose.build.yaml config --quiet
docker compose -f compose.build.yaml up -d --build
```

`.env`의 MEDIA_PATH를 실제 존재하는 테스트 폴더로, PUID/PGID를 해당 폴더 수정 권한이 있는 UID/GID로 맞추세요. BIND_ADDRESS 기본값은 `0.0.0.0`, 포트는 `18080`이므로 `http://NAS_IP:18080`으로 접속합니다. 설치용 Compose는 이미지 pull, 개발용 Compose는 소스 build로 각각 독립된 파일입니다. 일반 사용자에게 `build`를 요구하지 않습니다.

이미 이전 `adname` 테스트 컨테이너가 같은 포트를 쓰면 해당 배포 경로에서 중지한 뒤 실행하거나 다른 포트를 선택하세요. 기존 계정 설정을 유지하려면 CONFIG_PATH가 같은지 확인합니다.

## GitHub에 첫 업로드

1. GitHub에서 **MaratangNearl/svr-file-renamer**라는 빈 **Public** 저장소를 만듭니다. README 자동 생성은 선택하지 않습니다. 배포 라이선스는 제작자가 결정해 `LICENSE`로 추가할 수 있습니다.
2. 현재 프로젝트에서 공개 파일을 내보냅니다.

```bash
python3 scripts/prepare-release.py
```

출력은 `release/public/`과 `release/public.tar.gz`입니다. 이미 출력이 있으면 덮어쓰지 않습니다. 다음 내보내기는 `python3 scripts/prepare-release.py --output release/public-v2`처럼 새 폴더를 지정하세요.

3. 내보낸 파일만 검토하고 Git을 시작합니다. 아래 명령은 GitHub SSH 인증이 설정된 호스트에서 실행하세요. code-server가 컨테이너라면 이 폴더를 호스트로 복사하거나, 같은 인증이 가능한 터미널에서 실행합니다. 토큰·개인 SSH 키를 프로젝트 파일에 넣지 마세요.

```bash
cd release/public
git init -b main
git add .
git status --short
git diff --cached --stat
git diff --cached
```

`git status`에 PUTTY 안내, `.env`, DB, 미디어, 백업이 없어야 합니다. `git config user.name`과 `git config user.email`에는 본인의 GitHub 커밋 정보를 설정하세요. 값은 문서에서 임의로 지정하지 않습니다.

```bash
git commit -m "Initial release of SVR FILE RENAMER"
git remote add origin git@github.com:MaratangNearl/svr-file-renamer.git
git push -u origin main
```

GitHub SSH 인증이 없다면 GitHub CLI의 `gh auth login` 또는 GitHub의 SSH 키 등록 절차를 먼저 완료합니다. 저장소가 이미 있다면 새 이력으로 강제 push하지 말고 기존 checkout에 공개 파일을 반영하세요.

이후 수정은 이 공개 저장소 checkout에서 진행하면 됩니다. 개인 작업 공간에서 내보내기를 계속 사용한다면 새 출력 폴더를 생성하고 공개 checkout에 변경을 반영한 뒤 `git diff`로 삭제·추가를 함께 검토하세요.

## 첫 이미지 게시 — latest와 고정 버전

`.github/workflows/publish.yml`은 **정식 GitHub Release의 Published 이벤트**로 실행합니다. main에 push하는 것만으로 이미지는 공개하지 않습니다.

1. 저장소의 Actions 탭에서 `Test` workflow가 통과했는지 확인합니다.
2. Releases → Draft a new release에서 `v1.0.0` 같은 새 태그로 정식 릴리스를 게시합니다. Target은 검증한 main 커밋으로 지정합니다. Draft나 prerelease는 게시 대상이 아닙니다.
3. `Publish image and install files`가 테스트를 다시 실행하고 `linux/amd64`, `linux/arm64` 이미지를 GHCR에 올립니다. 기본 `GITHUB_TOKEN`의 packages:write/contents:write 권한을 사용하므로 별도 레지스트리 비밀번호를 코드에 넣지 않습니다. 조직 정책이 권한을 제한하면 저장소/조직 설정을 확인하세요.
4. 두 이미지 태그가 생성됩니다.

```text
ghcr.io/maratangnearl/svr-file-renamer:latest
ghcr.io/maratangnearl/svr-file-renamer:v1.0.0
```

5. **첫 게시 후 GitHub의 패키지 페이지 → Package settings → Change visibility → Public**을 확인하세요. 저장소가 Public이어도 패키지 공개 상태는 별도로 확인해야 합니다. 공개 이미지가 되어야 누구나 로그인 없이 pull할 수 있습니다.
6. 같은 릴리스에 `compose.yaml`과 `svr-file-renamer.env.example` 두 설치 파일이 첨부됐는지 확인합니다. 사용자는 README의 `releases/latest/download/...` 주소로 받습니다. 첫 정식 릴리스 성공 전에는 이 URL이 동작하지 않습니다.

공개 이미지 다운로드를 별도 PC 또는 인증 없는 Docker 설정으로 확인하세요. 계정 인증이 있는 개발 PC에서 pull 성공만 확인하면 공개 여부를 놓칠 수 있습니다.

다음 정식 릴리스를 게시하면 해당 버전 태그와 `latest`를 갱신합니다. 릴리스는 버전 순서대로 게시하세요. 오래된 버전을 나중에 새로 게시하면 그 이미지가 latest가 됩니다. 실패한 단계는 Actions에서 확인한 뒤 해당 workflow를 재실행할 수 있습니다. prerelease 이미지는 현재 게시하지 않습니다.

GitHub에서 릴리스 편집 화면에 임의 파일을 첨부하지 마세요. 자동 생성된 소스 압축에는 공개 저장소의 추적 파일만 들어가야 하므로 Git에 로컬 데이터를 올리지 않는 것이 중요합니다.

공식 참고: [GitHub 이미지 게시](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images), [GHCR 권한·공개 이미지](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## 운영 업데이트와 복구

일반 업데이트는 사용자의 `data`를 유지합니다. 버전별 초기화를 자동 실행하지 마세요. 새 테이블은 `CREATE TABLE IF NOT EXISTS`로 추가되며 기존 계정·TOTP·웹후크는 유지됩니다. 기존 일반 로그에서 변경 이력을 추정 복원하지 않습니다. 성공한 신규 변경부터 구조화된 이력을 기록합니다.

`reset-admin`은 계정/TOTP만, `reset-all`은 설정·로그·변경 이력까지 초기화합니다. 운영 서비스 중지 후 실행하며 영상 파일은 건드리지 않습니다. 자세한 명령은 사용자 README를 따릅니다. 설정 DB를 백업할 때는 SQLite WAL 일관성을 위해 서비스를 중지하거나 SQLite 백업 API를 사용하세요. 이전 버전 이미지로 되돌릴 때는 그 버전과 호환되는 DB 백업도 준비합니다.

변경 이력은 변경 작업과 DB 기록이 서로 다른 저장소에 있으므로 원자적 트랜잭션은 아닙니다. 파일 변경 직후 프로세스 강제 종료/디스크 오류가 나면 해당 변경이 이력에 남지 않을 수 있습니다. 이력은 자동 롤백 기능이 아닙니다.

## 현재 검증 범위

로컬에서는 Python 자동 테스트와 JS 문법·공개 패키지 검사를 실행합니다. 이 개발 환경에는 Docker가 없어 실제 이미지 빌드·멀티 아키텍처 실행은 확인하지 못했으며, GitHub CI/게시 workflow에서 확인해야 합니다. 브라우저 시스템 라이브러리 부재로 실제 렌더링과 상호작용 QA도 미완료입니다. Discord 자동 테스트는 HTTP 전송을 대체하며 실제 웹후크는 UI의 연결 테스트로 확인합니다.
