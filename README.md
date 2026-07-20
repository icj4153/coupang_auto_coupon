# coupang_auto_coupon

쿠팡 WING 화면을 Playwright로 자동 조작해 저장된 쿠폰을 매일 발급하는 NAS Docker 전용 자동화입니다.

## 구조

- `coupang-coupon-web`: 상품/쿠폰을 관리하는 웹폼
- `coupang-coupon-scheduler`: 전날 밤부터 실패분을 반복 재시도하며 쿠폰 발급 실행
- `data/`: NAS에 남는 쿠폰 목록, 상품 목록, 로그, 디버그 파일
- `nas_update.sh`: GitHub에서 최신 코드 pull 후 Docker 재빌드

웹폼은 쿠폰과 상품만 관리합니다. WING 로그인 정보는 NAS의 `.env`에만 넣습니다.

## NAS 최초 설치

DSM에 Git이 없어도 Docker로 clone할 수 있습니다.

```bash
cd /volume1/docker
/usr/local/bin/docker run --rm \
  -v /volume1/docker:/work \
  alpine/git \
  clone https://github.com/icj4153/coupang_auto_coupon.git /work/coupang_coupon

cd /volume1/docker/coupang_coupon
cp .env.example .env
vi .env
mkdir -p data/logs data/browser_artifacts
/usr/local/bin/docker-compose up -d --build
```

`.env` 필수값:

```bash
COUPANG_WING_ID=쿠팡WING아이디
COUPANG_WING_PASSWORD=쿠팡WING비밀번호
COUPON_WEB_USER=admin
COUPON_WEB_PASSWORD=긴_랜덤_비밀번호
COUPON_WEB_BIND=127.0.0.1
COUPANG_LOGIN_ATTEMPTS=5
COUPANG_LOGIN_RETRY_SECONDS=60
COUPON_TODAY_START_BUFFER_MINUTES=5
COUPON_PAUSE_ON_ACCESS_DENIED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
COUPON_SETUP_LOGIN_TIMEOUT_MINUTES=30
COUPON_VNC_PASSWORD=
COUPON_VNC_BIND=127.0.0.1
COUPON_NAS_LAN_HOST=192.168.50.101
MAC_WAKE_ON_FAILURE=false
MAC_WAKE_ADDRESS=
MAC_WAKE_BROADCAST=192.168.50.255
MAC_WAKE_PORT=9
```

`COUPON_WEB_BIND=127.0.0.1`은 NAS 내부와 역방향 프록시에서만 웹폼에 접근하게 하는 설정입니다. 외부 공개 시에는 반드시 `COUPON_WEB_PASSWORD`를 길고 예측 불가능하게 설정하세요.

`MAC_WAKE_ON_FAILURE=true`와 `MAC_WAKE_ADDRESS`를 설정하면 WING 로그인 차단/세션 만료 알림을 보낼 때 NAS가 Wake-on-LAN 패킷으로 Mac 깨우기를 시도합니다. Mac의 Wake for network access 설정과 공유기/내부망 환경에 따라 동작 여부가 달라질 수 있습니다.

## 접속 설정

기존 `https://icj7297.synology.me` 규칙을 건드리지 않으려면 별도 포트를 쓰는 방식이 가장 단순합니다.

라우터 포트포워딩:

```text
외부 TCP 8766 -> 192.168.50.101 TCP 8766
```

Synology 역방향 프록시:

```text
소스: HTTPS / icj7297.synology.me / 8766
대상: HTTP  / 127.0.0.1          / 8765
```

접속 주소:

```text
https://icj7297.synology.me:8766
```

## 매일 쓰는 법

1. 웹폼에 접속합니다.
2. `상품 추가`에서 옵션ID 묶음을 저장합니다. 여러 옵션ID는 줄바꿈으로 입력합니다.
3. `쿠폰 추가`에서 쿠폰 종류, 할인 방식, 행사명, 상품, 할인 조건을 저장합니다.
4. 자동화 쿠폰 목록 좌측 체크박스로 실행할 쿠폰을 선택합니다.
5. `선택 입력 테스트`로 WING 입력과 상품 조회까지 확인합니다.
6. 문제가 없으면 `선택 쿠폰 만들기 실행`으로 실제 발급합니다.

당일 쿠폰이 필요하면 자동화 쿠폰 목록에서 쿠폰을 선택하고 `당일 쿠폰 생성` 영역에 시작 시간을 입력합니다. `당일 입력 테스트`는 실제 발급 없이 입력과 상품 조회까지만 확인하고, `당일 쿠폰 만들기`는 입력한 시간부터 당일 23:59까지 실제 발급합니다. 시작 시간은 현재 시각보다 뒤여야 합니다.

스케줄러 컨테이너가 켜져 있으면 전날 `22:30`에 다음날 쿠폰 생성을 시작합니다. 실패한 쿠폰은 전날 `22:50`, `23:10`, `23:30`, `23:50`에 다시 시도하고, 자정 이후에도 남아 있으면 당일 `00:05`, `01:05`, `02:05`, `04:05`, `08:05`, `12:05`에 긴급 복구합니다.

자동 실행은 저장된 로그인 세션을 먼저 사용합니다. 세션이 만료된 경우 환경변수 ID/PW로 로그인하며, 일시적인 `Access Denied`가 발생하면 기본 60초부터 점진 대기하며 최대 5회 로그인 재시도합니다.

`COUPON_PAUSE_ON_ACCESS_DENIED=true`이면 WING `Access Denied` 또는 로그인 세션 만료가 감지될 때 `/data/logs/automation_paused.json`을 만들고 자동 재시도를 멈춥니다. Mac에서 `refresh_wing_session.command`로 세션 업로드가 성공하면 이 일시정지는 자동으로 해제되고 스케줄러가 다시 켜집니다.

## WING 로그인 세션 갱신

쿠팡이 NAS/Linux 또는 Playwright 로그인 화면을 `Access Denied`로 막으면 자동화 브라우저에서 새 로그인 세션을 만들 수 없습니다. 이 경우 Mac의 일반 Chrome 앱에서 직접 WING에 로그인한 뒤, 그 Chrome 세션을 Playwright storage state 형식으로 export해서 NAS로 업로드합니다.

Mac에서 더블클릭:

```text
refresh_wing_session.command
```

직접 명령으로 실행:

```bash
cd /Users/joon/Desktop/coupang
./refresh_wing_session.command
```

스크립트 흐름:

1. 일반 Chrome이 로그인 전용 프로필 `.chrome-wing-login-profile/`로 열립니다.
2. WING 로그인을 완료한 뒤 터미널에서 Enter를 누릅니다.
3. Chrome 세션을 읽어 Mac의 `wing_storage_state.json`을 새로 저장합니다.
4. 세션 파일을 NAS의 `/volume1/docker/coupang_coupon/data/wing_storage_state.server.json`로 업로드합니다.
5. 원하면 바로 NAS에서 실패/미완료 쿠폰 복구를 실행합니다.

이 방식은 로그인 화면 조작을 Playwright로 하지 않습니다. Playwright는 로그인 완료 후 Chrome에 연결해서 쿠키를 세션 파일로 저장하는 역할만 합니다.

Mac이 잠들어 있을 수 있다면 시스템 설정에서 `Wake for network access`를 켜고, 필요 시 공유기/앱/DSM Wake-on-LAN으로 Mac을 먼저 깨운 뒤 위 스크립트를 실행합니다.

텔레그램 실패 알림이 `Access Denied` 또는 세션 만료로 판단되면 `refresh_wing_session.command` 실행을 안내합니다. `MAC_WAKE_ON_FAILURE=true`와 `MAC_WAKE_ADDRESS`가 설정되어 있으면 알림 발송 시 Mac 깨우기 패킷도 함께 보냅니다.

전날 생성되는 쿠폰 유효기간은 다음날 `00:00`부터 `23:59`까지입니다. 당일 긴급 복구로 생성되는 쿠폰은 현재 시각 기준 `COUPON_TODAY_START_BUFFER_MINUTES`분 뒤부터 당일 `23:59`까지입니다.

여러 쿠폰 중 하나가 실패해도 뒤 쿠폰은 계속 시도합니다. 성공한 쿠폰은 `/data/logs/coupon_run_status.json`에 기록되어 다음 재시도에서 제외되고, 실패/미완료 쿠폰만 다시 실행됩니다. 실패 항목이 있으면 `browser_artifacts/`에 해당 쿠폰의 스크린샷과 HTML이 저장됩니다.

`TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`를 설정하면 첫 실패, 일부 실패, 복구 성공, 최종 실패 시 텔레그램 알림을 보냅니다.

## 상태 확인

```bash
cd /volume1/docker/coupang_coupon
/usr/local/bin/docker-compose ps
/usr/local/bin/docker-compose logs --tail=80 coupang-coupon-web
/usr/local/bin/docker-compose logs --tail=80 coupang-coupon-scheduler
```

스케줄러가 정상 대기 중이면 아래처럼 보입니다.

```text
[nas-scheduler] Scheduler started. Retry slots: previous day 22:30/22:50/23:10/23:30/23:50, same day 00:05/01:05/02:05/04:05/08:05/12:05 Asia/Seoul
[nas-scheduler] Each run creates only coupons that are not yet marked successful.
```

실제 발급 없이 계획만 확인:

```bash
/usr/local/bin/docker-compose exec coupang-coupon-scheduler python3 daily_coupon_runner.py --dry-run
```

## 업데이트

GitHub에 새 코드가 올라간 뒤 NAS에서 실행합니다.

```bash
cd /volume1/docker/coupang_coupon
sh nas_update.sh
```

NAS에 deploy key를 아직 넣지 않았다면 HTTPS pull로 임시 업데이트할 수 있습니다.

```bash
cd /volume1/docker/coupang_coupon
/usr/local/bin/docker run --rm \
  -v /volume1/docker/coupang_coupon:/repo \
  alpine/git \
  -C /repo \
  -c safe.directory=/repo \
  pull --ff-only

/usr/local/bin/docker-compose up -d --build
```

## GitHub Actions 배포

`.github/workflows/deploy-nas.yml`은 NAS에 SSH 접속해서 `nas_update.sh`를 실행합니다.

GitHub Secrets:

- `NAS_HOST`
- `NAS_PORT`
- `NAS_USER`
- `NAS_SSH_PRIVATE_KEY`
- `NAS_KNOWN_HOSTS`

NAS deploy key 위치:

```text
/volume1/docker/coupang_coupon_secrets/github_deploy_key
```

## 파일 위치

NAS 기준 주요 파일:

- `/volume1/docker/coupang_coupon/.env`
- `/volume1/docker/coupang_coupon/data/coupon_products.json`
- `/volume1/docker/coupang_coupon/data/coupon_form_coupons.json`
- `/volume1/docker/coupang_coupon/data/logs/`
- `/volume1/docker/coupang_coupon/data/browser_artifacts/`

민감정보와 실행 데이터는 Git에 올리지 않습니다.

## 오류 확인

자동화 실패 시 먼저 로그를 봅니다.

```bash
/usr/local/bin/docker-compose logs --tail=120 coupang-coupon-scheduler
ls -lah data/logs
ls -lah data/browser_artifacts
```

쿠팡이 추가 인증, 보안 확인, 새 기기 확인을 요구하면 자동 로그인이 실패할 수 있습니다. 이 경우 `browser_artifacts/`의 스크린샷과 HTML을 확인한 뒤 WING 계정 상태를 점검해야 합니다.
