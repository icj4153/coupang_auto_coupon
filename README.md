# coupang_auto_coupon

쿠팡 WING 화면을 Playwright로 자동 조작해 저장된 쿠폰을 매일 발급하는 NAS Docker 전용 자동화입니다.

## 구조

- `coupang-coupon-web`: 상품/쿠폰을 관리하는 웹폼
- `coupang-coupon-scheduler`: 매일 `COUPON_DAILY_TIME`에 쿠폰 발급 실행
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
```

`COUPON_WEB_BIND=127.0.0.1`은 NAS 내부와 역방향 프록시에서만 웹폼에 접근하게 하는 설정입니다. 외부 공개 시에는 반드시 `COUPON_WEB_PASSWORD`를 길고 예측 불가능하게 설정하세요.

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

스케줄러 컨테이너가 켜져 있으면 매일 `00:01`에 저장된 전체 쿠폰을 그날 날짜 기준으로 발급합니다. 시간은 `.env` 또는 `docker-compose.yml`의 `COUPON_DAILY_TIME`으로 바꿀 수 있습니다.

## 상태 확인

```bash
cd /volume1/docker/coupang_coupon
/usr/local/bin/docker-compose ps
/usr/local/bin/docker-compose logs --tail=80 coupang-coupon-web
/usr/local/bin/docker-compose logs --tail=80 coupang-coupon-scheduler
```

스케줄러가 정상 대기 중이면 아래처럼 보입니다.

```text
[nas-scheduler] Scheduler started. Daily run time: 00:01 Asia/Seoul
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
