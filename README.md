# Coupang WING Coupon Browser Automation

스윕 같은 발주 자동화 서비스를 유지한 채, 쿠팡 WING 화면을 Playwright 브라우저 자동화로 조작해 하루짜리 쿠폰 발급을 돕는 스크립트입니다.

## 매일 쓰는 법

1. `쿠폰 웹폼.command`를 더블클릭합니다.
2. `상품 추가` 버튼으로 옵션ID 묶음을 먼저 저장합니다. 여러 옵션ID는 줄바꿈으로 입력합니다.
3. `쿠폰 추가` 버튼으로 쿠폰 종류(즉시할인쿠폰/다운로드쿠폰), 할인 방식(정액/정률), 행사명, 상품, 할인 조건을 저장합니다. 행사명은 쿠폰명에 그대로 들어가며 날짜가 자동으로 붙지 않습니다.
4. 목록 좌측 체크박스로 실행할 쿠폰을 선택합니다.
5. `선택 입력 테스트`를 눌러 쿠팡 WING에 값이 제대로 들어가는지 확인합니다.
6. 문제가 없으면 웹폼에서 `선택 쿠폰 만들기 실행`을 누릅니다.
7. 로그인 만료 에러가 나면 웹폼의 `로그인 정보 저장` 또는 `수동 로그인`으로 다시 준비합니다.

실행 로그는 `logs/` 폴더에 날짜별로 저장됩니다. 화면 입력 실패 시 디버그용 스크린샷과 HTML은 `browser_artifacts/`에 저장됩니다.

## 매일 00:01 자동 실행

웹폼에서 `로그인 정보 저장`을 눌러 쿠팡 WING ID/PW를 macOS Keychain에 저장합니다. 비밀번호는 프로젝트 파일에 저장하지 않습니다.

그다음 `매일 00:01 자동 실행 설치`를 누르면 macOS `launchd`가 매일 00:01에 저장된 자동화 쿠폰 목록을 그날 날짜 기준으로 실행합니다.

자동 실행 조건:

- Mac이 켜져 있고 해당 사용자로 로그인된 상태여야 합니다.
- 쿠팡이 추가 인증, 보안문자, OTP 등을 요구하면 자동 로그인이 멈출 수 있습니다.
- 결과 로그는 `logs/YYYY-MM-DD_daily_coupon.log`에 저장됩니다.
- 설치된 자동 실행은 웹폼의 `자동 실행 해제`로 끌 수 있습니다.

실제 발급 없이 자동 실행 계획만 확인하려면:

```bash
python3 daily_coupon_runner.py --dry-run
```

실제 발급은 안전상 명시적으로 `--run`이 있어야만 실행됩니다.

## Mac 없이 서버에서 자동 실행

Mac을 켜두기 싫다면 Linux VM 한 대에서 실행하는 방식이 가장 현실적입니다. Vercel, GitHub Actions 같은 서버리스/CI 환경은 브라우저 로그인, 보안 인증, 긴 실행 시간, 고정 세션 관리에 약해서 추천하지 않습니다.

서버 실행은 매일 저장 세션을 재사용하지 않고 새 브라우저 컨텍스트에서 로그인합니다. 일일 실행 스크립트가 내부적으로 `--auto-login --fresh-login`을 붙여 실행합니다.

서버 준비 예시:

```bash
cd /opt/coupang-coupon
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/playwright install chromium --with-deps
cp browser_coupon_config.server.example.json browser_coupon_config.json
```

서버 로그인 정보는 환경변수로 넣습니다. 예시는 `server.env.example`에 있습니다.

```bash
sudo cp server.env.example /etc/coupang-coupon.env
sudo nano /etc/coupang-coupon.env
sudo chmod 600 /etc/coupang-coupon.env
```

서버 시간이 한국 시간인지 확인합니다. systemd 타이머는 서버 로컬 시간을 기준으로 돌기 때문에, 가능하면 서버 타임존을 Asia/Seoul로 맞춥니다.

```bash
timedatectl
sudo timedatectl set-timezone Asia/Seoul
```

실제 발급 없이 서버 실행 계획만 확인합니다.

```bash
. /etc/coupang-coupon.env
python3 daily_coupon_runner.py --dry-run
```

매일 00:01 자동 실행은 `systemd/` 폴더의 예시 파일을 복사해서 등록합니다. 예시 파일의 `/opt/coupang-coupon` 경로는 실제 설치 경로와 맞아야 합니다.

```bash
sudo cp systemd/coupang-coupon.service.example /etc/systemd/system/coupang-coupon.service
sudo cp systemd/coupang-coupon.timer.example /etc/systemd/system/coupang-coupon.timer
sudo systemctl daemon-reload
sudo systemctl enable --now coupang-coupon.timer
systemctl list-timers coupang-coupon.timer
```

서버 자동 로그인은 쿠팡이 추가 인증, 보안문자, OTP, 새 기기 확인을 요구하면 멈출 수 있습니다. 이 경우 `logs/YYYY-MM-DD_daily_coupon.log`와 `browser_artifacts/`를 확인해야 합니다.

## NAS Docker 배포

`b2b_excel` 프로젝트의 NAS 배포 흐름과 같은 방식으로 구성했습니다.

- GitHub Actions: `.github/workflows/deploy-nas.yml`
- NAS 업데이트 스크립트: `nas_update.sh`
- Docker Compose: `docker-compose.yml`
- 웹 UI 컨테이너: `coupang-coupon-web`
- 매일 00:01 실행 컨테이너: `coupang-coupon-scheduler`
- 영구 데이터 폴더: `data/`

DS220+는 Intel x86_64 모델이라 Playwright Chromium Docker 구성이 가능합니다. 기본 메모리 2GB 상태에서도 가벼운 단일 실행은 가능할 수 있지만, DSM과 다른 패키지를 같이 쓰면 여유가 빡빡할 수 있습니다. 가능하면 RAM 업그레이드가 안정적입니다.

Chromium 안정성을 위해 `docker-compose.yml`에는 `shm_size: "1gb"`와 `init: true`를 넣어두었습니다.

NAS 최초 준비 예시:

```bash
cd /volume1/docker
git clone <이 저장소 SSH URL> coupang_coupon
cd /volume1/docker/coupang_coupon
cp .env.example .env
vi .env
sh nas_update.sh
```

`.env`에는 쿠팡 WING 로그인 정보를 넣습니다.

```bash
COUPANG_WING_ID=쿠팡WING아이디
COUPANG_WING_PASSWORD=쿠팡WING비밀번호
COUPON_WEB_BIND=127.0.0.1
```

`COUPON_WEB_BIND=127.0.0.1`이면 NAS 내부 또는 리버스 프록시를 통해서만 웹 UI에 접근합니다. 내부망에서 직접 접속해야 하면 `0.0.0.0`으로 바꿀 수 있지만, 로그인 정보가 걸린 도구라 외부 공개는 피하는 편이 좋습니다.

GitHub Secrets는 기존 `b2b_excel`과 같은 이름을 씁니다.

- `NAS_HOST`
- `NAS_PORT`
- `NAS_USER`
- `NAS_SSH_PRIVATE_KEY`
- `NAS_KNOWN_HOSTS`

NAS 쪽 Git deploy key 위치도 기존 패턴과 같습니다.

```text
/volume1/docker/coupang_coupon_secrets/github_deploy_key
```

컨테이너 상태 확인:

```bash
docker-compose ps
docker-compose logs --tail=80 coupang-coupon-web
docker-compose logs --tail=80 coupang-coupon-scheduler
```

실제 발급 없이 확인:

```bash
docker-compose exec coupang-coupon-scheduler python3 daily_coupon_runner.py --dry-run
```

## GitHub 연동 순서

이 폴더는 민감정보가 올라가지 않도록 `.gitignore`로 쿠폰 데이터, 상품 옵션ID, 로그인 세션, 로그, `.env`, `data/`를 제외합니다.

GitHub에 빈 저장소를 만든 뒤, 로컬에서 원격만 연결하면 됩니다.

```bash
git remote add origin git@github.com:<계정>/<저장소>.git
git push -u origin main
```

push 후 GitHub 저장소의 `Settings > Secrets and variables > Actions`에 아래 값을 추가합니다.

- `NAS_HOST`
- `NAS_PORT`
- `NAS_USER`
- `NAS_SSH_PRIVATE_KEY`
- `NAS_KNOWN_HOSTS`

NAS에는 기존 `b2b_excel`처럼 deploy key를 둡니다.

```text
/volume1/docker/coupang_coupon_secrets/github_deploy_key
```

## 준비

처음 한 번 설정 파일을 만듭니다.

```bash
cp browser_coupon_config.json.example browser_coupon_config.json
cp browser_coupons.csv.example browser_coupons.csv
```

로그인 세션을 저장합니다. 브라우저가 열리면 쿠팡 WING에 직접 로그인한 뒤, WING 메인/대시보드가 완전히 보이면 터미널에서 Enter를 누릅니다. 성공하면 `wing_storage_state.json`이 생성됩니다.

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --setup-login
```

로그인이 저장됐는지 확인하려면 진단 모드를 실행합니다.

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --inspect
```

진단 모드가 계속 로그인 화면으로 이동하면 다시 로그인 세션을 저장합니다.

```bash
rm -f wing_storage_state.json
python3 wing_coupon_browser.py --config browser_coupon_config.json --setup-login
```

웹폼을 쓰면 CSV를 직접 수정하지 않아도 됩니다. 웹폼이 저장된 쿠폰 목록 중 선택한 항목만 `browser_coupons.generated.csv`로 자동 생성해 실행합니다. 저장된 쿠폰 목록은 `coupon_form_coupons.json`, 상품 묶음은 `coupon_products.json`에 보관됩니다.

터미널이나 기존 `.command` 파일로 직접 실행하고 싶다면 쿠폰 조건을 `browser_coupons.csv`에 넣습니다. 여러 옵션 ID는 `;`로 구분합니다. 실행할 때는 쿠팡 입력칸에 줄바꿈으로 자동 입력됩니다.

예:

```csv
enabled,campaign_name,coupon_kind,vendor_item_ids,discount_type,discount,min_order_price,max_discount_price,max_issue_count
true,오늘만 특가,downloadable,"123456;234567;345678",PRICE,2000,5000,,2
```

터미널에서 직접 실행하려면 아래 명령을 사용합니다. 실제 제출 없이 화면 입력과 스크린샷만 확인합니다.

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --csv browser_coupons.csv
```

기본 설정은 쿠팡 WING 쿠폰 작성 URL로 바로 이동합니다. 자동 이동이 실패했을 때만 수동 쿠폰 페이지 모드를 사용합니다. 브라우저에서 직접 쿠폰 작성 화면까지 이동한 뒤 터미널에서 Enter를 누르면, 그 화면부터 자동 입력을 시작합니다.

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --csv browser_coupons.csv --manual-coupon-page
```

정상 입력이 확인되면 실제 제출까지 실행합니다.

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --csv browser_coupons.csv --submit
```

수동 쿠폰 페이지 모드에서 제출까지 실행하려면:

```bash
python3 wing_coupon_browser.py --config browser_coupon_config.json --csv browser_coupons.csv --manual-coupon-page --submit
```

WING 화면에서 입력칸을 못 찾으면 `browser_artifacts/`에 스크린샷과 HTML이 저장됩니다. 그때 `browser_coupon_config.json`의 `selectors`에 해당 입력칸 CSS selector를 넣어 보정하면 됩니다.
