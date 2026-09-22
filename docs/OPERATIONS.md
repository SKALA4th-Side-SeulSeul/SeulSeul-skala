# 운영자 명령 빠른 안내

운영 서버 `~/app`에서 실행합니다. 가장 먼저 `./admin.sh`를 사용하면 대시보드와 조치 메뉴를 한 번에 엽니다.

## 매일 확인

| 명령 | 용도 |
| --- | --- |
| `./admin.sh` | 대시보드 + 번호 선택형 관리자 메뉴 |
| `./view.sh dashboard` | 공지 처리 현황과 조치 목록 1회 조회 |
| `./view.sh dashboard --watch` | 대시보드 5초마다 갱신. 종료는 `Ctrl+C` |
| `./view.sh logs bot --follow` | 봇 로그 최근 내용부터 실시간 추적 |
| `./view.sh logs bot --tail 300` | 과거 봇 로그 300줄 조회 |
| `./view.sh logs bot --raw` | 원본 로그·trace·식별자 확인 |
| `./view.sh` | 컨테이너 상태 확인 |

대시보드의 처리 필요 목록 번호는 `./retry.sh retry --index N`에 그대로 사용합니다. `./retry.sh list`도 같은 번호를 표시합니다.

## 장애 처리 순서

| 화면에 보이는 상태 | 관리자 조치 |
| --- | --- |
| `자동 재시도 대기` | 기다립니다. 봇이 5분·15분·1시간 간격으로 자동 재처리합니다. |
| `수동 조치 필요` | 아래 번호로 AI 재처리합니다. |
| AI 결과가 잘못됨 | `./notice.sh` → `수정` → 수동 제목·요약·마감일 입력 |
| `미적용 원본` | `./retry.sh pending --limit 100`으로 확인합니다. 계속 남으면 Slack 원문을 수정·재게시하거나 `./notice.sh`로 처리합니다. |
| 봇 동작 확인 필요 | `./view.sh logs bot --tail 300` 후 필요하면 `./view.sh logs bot --follow` |

### 실패 공지 AI 재처리

```bash
./view.sh dashboard
./retry.sh retry --index 1 --limit 100
```

`N`은 대시보드 또는 `./retry.sh list --limit 100`의 번호입니다. `retry.sh`는 AI를 다시 호출합니다. 수동 제목·요약·마감일을 입력하는 명령이 아닙니다. 재처리 성공 후 실행 중인 봇이 기존 체크리스트와 DM을 갱신합니다.

번호 대신 식별자를 직접 지정해야 하는 경우에만 아래 형식을 사용합니다.

```bash
./retry.sh retry \
  --workspace-id '<워크스페이스 ID>' \
  --url '<공지의 제출 링크>' \
  --channel-id '<원본 채널 ID>' \
  --message-ts '<원본 메시지 ts>'
```

### AI 결과 수동 수정

```bash
./notice.sh
```

`수정`을 선택하고 공지를 고른 뒤 바꿀 값만 입력합니다. 같은 원문에 제출 링크가 여러 개면 수정할 링크를 번호로 고릅니다. 마지막 확인에서 `y`를 입력해야 저장됩니다. `Enter`, `q`, `Ctrl+C`는 취소 또는 기존 값 유지입니다.

### 미적용 원본 확인

```bash
./retry.sh pending --limit 100
```

이 목록은 이벤트가 정상 처리 중인 원본도 포함할 수 있습니다. 잠시 후 다시 확인하고, 계속 남으면 Slack에서 원문을 실제로 수정해 새 이벤트를 발생시키거나 `./notice.sh`에서 수동 처리합니다. 과거 Slack 기록 전체를 자동으로 다시 읽지는 않습니다.

## 관리자 메뉴

```bash
./admin.sh
```

| 선택 | 동작 |
| --- | --- |
| `1` | 번호 입력 → `y` 확인 → `retry --index` 실행 |
| `2` | `notice.sh` 수동 등록·수정·삭제 |
| `3` | 미적용 원본 조회 |
| `4` | `./view.sh logs bot --follow` 실행 |
| `r` | 대시보드 새로고침 |
| `q` | 종료 |

## 공지 수동 등록·수정·삭제

```bash
./notice.sh
```

봇을 추가로 시작하지 않고 일회성 CLI 컨테이너만 실행합니다. 등록·수정·삭제는 저장 전 확인하며, 원문·링크·학생별 완료 기록을 기존 서비스 규칙으로 보존합니다.

호환용 직접 명령은 다음과 같습니다.

```bash
./notice_edit.sh
./retry.sh list
./retry.sh pending
```

## 운영 시작·중지·백업

| 명령 | 용도 |
| --- | --- |
| `./run.sh` | 이미지 빌드, DB 준비, 마이그레이션, bot·OAuth·ngrok 재시작 |
| `./stop.sh` | 서비스 중지. 데이터 볼륨은 삭제하지 않음 |
| `./view.sh config` | 설정값을 출력하지 않고 Compose 구성 검증 |
| `./backup.sh` | 운영 DB 백업 |
| `./backup_run.sh` | 매일 03:00 자동 백업 등록 |
| `./backup_stop.sh` | 자동 백업 해제 |

`.env` 변경 후에는 `./run.sh`로 컨테이너를 재생성해야 반영됩니다. 운영 DB를 직접 수정하거나 Docker Compose로 두 번째 봇을 실행하지 않습니다.
