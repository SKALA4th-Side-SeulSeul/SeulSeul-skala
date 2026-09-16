# SeulSeul

Slack 공지를 분석해 학생별 체크리스트를 만들고, 마감일에 맞춰 DM으로 알려주는 Slack 봇입니다.

## 개발 시작

Python 3.11을 사용합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

`.env`에 개발용 `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL`을 채운 뒤 PostgreSQL과 마이그레이션을 준비합니다.

```bash
docker compose up -d postgres
.venv/bin/alembic upgrade head
```

로컬에서는 PostgreSQL만 Docker로 실행하고 Python 앱은 `.venv`에서 실행합니다. 개발 DB를 멈출 때는 데이터 볼륨을 보존하도록 `docker compose down`을 사용하며, 데이터 초기화가 목적이 아니면 `-v`를 붙이지 않습니다.

Slack 앱 설정은 `slack-manifest.yaml`을 기준으로 맞춥니다. `users:read` 같은 OAuth scope를 새로 적용했다면 워크스페이스에 앱을 다시 설치한 뒤 갱신된 Bot Token을 `.env`에 반영합니다.

가입 시 Slack 프로필의 **성명**을 `4기_광주_<1~4>반_<이름>` 형식으로 설정하고 `/seulseul 시작`을 실행합니다. 소속은 성명(`profile.real_name`)으로 판별하며, 표시 이름은 자유롭게 사용할 수 있습니다. 기존 가입자도 같은 명령으로 성명과 반을 갱신할 수 있습니다.

운영 서버에서는 `compose.prod.yaml`을 사용합니다. 운영 절차와 보안 기준은 `docs/DB-TODO.md`를 따릅니다.

## 매일 오전 9시 개인 체크리스트

학생이 입력하는 명령은 `/seulseul 시작`, `/seulseul 해지`뿐입니다. 가입·해지 결과는 본인에게만 보이는 임시 안내이고, 체크리스트는 봇과 학생의 **일반 DM**으로 전달됩니다. `/seulseul`만 입력하거나 다른 하위 명령을 입력하면 사용법을 안내합니다.

실행 전 다음 설정을 적용합니다.

1. `.env`의 `SLACK_NOTICE_TARGETS`에 `SLACK_NOTICE_CHANNELS`의 각 채널 대상을 지정합니다. `all`은 등록된 광주 학생 전체, `1`~`4`는 해당 반입니다. 아래는 가상 ID 예시이며 실제 값으로 바꿉니다. 채널의 순서로 대상을 추정하지 않습니다.

   ```dotenv
   SLACK_NOTICE_TARGETS=C0000000001=all,G0000000002=all,C0000000003=3
   ```

2. `slack-manifest.yaml`의 `chat:write`, `im:write` 권한과 명령 안내를 Slack 앱에 반영하고 재설치합니다. 토큰이 바뀌었다면 `.env`도 갱신합니다.
3. 봇을 멈춘 상태에서 마이그레이션 후 다시 실행합니다. 기존 학생·공지·완료 상태는 유지하고 일일 DM 발송 기록 테이블만 추가합니다.

   ```bash
   .venv/bin/alembic upgrade head
   .venv/bin/python -m seulseul.main
   ```

- 시간대는 `Asia/Seoul`입니다. 매일 오전 9시부터 학생별 당일 메시지를 한 번 발송하며, 할 일이 없어도 빈 목록을 보냅니다. 봇은 단일 프로세스로 상시 실행해야 합니다.
- 기본 목록은 본인에게 배정된 **마감 전·미삭제·미완료** 항목입니다. 마감이 빠른 순으로 5개씩 표시하고, 제출 링크·Slack 원문 링크·완료하기 버튼을 제공합니다. `완료 항목 보기`에서 마감 전 완료 항목을 취소할 수 있습니다.
- 새 공지 분석 성공, 가입, 완료·취소 시 갱신 작업을 깨웁니다. 기본 30초 주기 검사로 CLI 재처리 성공, 마감 경과, 복구 후 당일 미발송분도 반영합니다. 이미 보낸 날의 메시지가 있으면 새로 보내지 않고 수정합니다.
- 0~9시에는 공지를 배정만 합니다. 9시 이후 가입하면 마감 전 기존 공지도 배정하고 당일 DM을 생성합니다. 반 변경 시 현재 반에 해당하는 항목만 보여 주며 과거 완료 기록은 초기화하지 않습니다.
- 이전 날짜의 버튼이나 타인의 항목으로 상태를 바꿀 수 없습니다. 해지하면 개인 체크리스트와 일일 발송 기록을 DB에서 삭제합니다. Slack에 이미 전달된 DM 자체는 남지만 버튼은 사용할 수 없습니다.
- 전송이 명확히 실패하면 지연 후 다시 시도합니다. 첫 전송 후 응답 유실·프로세스 중단으로 성공 여부가 불명확하면 `daily_checklist_messages.status=uncertain`으로 기록하며 **자동 재발송하지 않습니다**. 로그의 `delivery` ID와 Slack 메시지 존재 여부를 운영자가 대조해야 합니다. 결과 확인 없이 상태를 초기화하면 중복 DM이 생길 수 있습니다.
- 실제 Slack 공지의 수정·삭제 이벤트 동기화는 아직 별도 작업입니다. 현재는 DB에 기록된 삭제 상태와 마감 경과를 화면에서 제외합니다.

## AI 분석 실패 공지 재처리

AI 서버를 켜도 이미 저장된 실패 상태는 자동으로 바뀌지 않습니다. 같은 링크를 다시 게시하면
중복으로 건너뛰므로, 운영자는 프로젝트 폴더에서 아래 명령으로 실패 목록을 확인합니다.

```bash
.venv/bin/python -m seulseul.notices.retry list
```

목록에 표시된 **재처리 명령**을 복사해 실행하면 해당 공지 하나를 다시 분석합니다.
명령 형식은 다음과 같습니다. URL은 Slack 원문 링크가 아니라 공지에 포함된 제출 링크입니다.

```bash
.venv/bin/python -m seulseul.notices.retry retry --workspace-id '<워크스페이스 ID>' --url '<제출 링크>'
```

- 현재 `.env`/환경변수의 DB·공지 채널·AI 설정을 사용합니다. 설정의 우선순위는 `config.py`를 따릅니다.
- 목록 조회는 AI를 호출하지 않습니다. 기본 20건이며 `--limit 100`, `--workspace-id`로 조정합니다.
- 성공하면 기존 공지를 갱신하고 오류를 지웁니다. 다시 실패하면 최근 오류를 저장합니다.
- 저장된 원문과 원래 게시 시각을 사용합니다. Slack의 수정된 원문을 새로 가져오지는 않습니다.
- 이미 성공했거나 삭제된 공지, 현재 설정에서 제외된 채널의 공지는 재처리하지 않습니다.
- 재처리에 성공하면 실행 중인 봇의 주기 작업이 배정하고 당일 DM을 갱신합니다. 오전 9시 이전에는 9시 발송분에 반영합니다. 위 최초 설정 이후 재처리할 때마다 앱을 재설치할 필요는 없습니다.
- 학생용 Slack 재처리 명령은 제공하지 않습니다.

## 검증

```bash
./scripts/check.sh
```

문법 검사, 테스트, Ruff 검사와 포맷 검사를 순서대로 실행합니다. 가상환경을 활성화하지 않아도 프로젝트 `.venv`를 사용합니다.

검증 전에 `.venv`를 자동으로 준비합니다. `.venv`가 없거나 Python 3.11이 아니면 다시 만들고, 개발 의존성이 없거나 `pyproject.toml`이 바뀌었으면 다시 설치합니다. Python 3.11 자체는 미리 설치되어 있어야 합니다.

Ruff 검사나 포맷 검사가 실패하면 다음 명령으로 자동 수정한 뒤 다시 검증합니다.

```bash
.venv/bin/python -m ruff check --fix .
.venv/bin/python -m ruff format .
```

## 문서

문서 목록은 `AGENTS.md`의 "기준 문서"를 참고합니다.
