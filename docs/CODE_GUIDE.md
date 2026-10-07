# SeulSeul 코드 가이드

처음 코드를 읽는 사람이 **Slack 이벤트가 어디로 들어와 어떤 규칙으로 DB와 DM을 바꾸는지** 빠르게 찾기 위한 안내서입니다. 제품 정책은 [PRODUCT.md](PRODUCT.md), 모듈 경계는 [ARCHITECTURE.md](ARCHITECTURE.md), 운영 명령은 [OPERATIONS.md](OPERATIONS.md)를 기준으로 합니다.

## 1. 30초 요약

- 봇은 Slack Socket Mode로 이벤트를 받습니다. 따라서 일반 봇 동작에는 공개 HTTP URL이 필요하지 않습니다.
- `slack/handlers.py`는 입력을 확인하고 도메인 서비스만 호출합니다. DB나 외부 API를 직접 다루지 않습니다.
- `notices/service.py`가 공지 URL·채널·원본 버전을 관리하고, `ai/service.py`가 AI 결과를 검증합니다.
- `checklists/service.py`가 학생별 표시 대상을 결정하고, 같은 개인 DM을 새로 보내지 않고 갱신합니다.
- `jobs/scheduler.py`가 30초 주기로 공지 재시도와 체크리스트 갱신을 실행하며, 공지·학생 변경 시 즉시 깨울 수도 있습니다.
- 외부 워크스페이스 설치만 `oauth` + `ngrok` 경로를 사용합니다. OAuth가 저장한 설치 정보는 PostgreSQL이 아니라 전용 볼륨에 둡니다.

## 2. 처음 읽을 순서

| 순서 | 파일 | 먼저 볼 이유 |
| --- | --- | --- |
| 1 | `src/seulseul/main.py` | 객체 조립, AI 설정, 핸들러 등록, 두 스케줄러 시작 |
| 2 | `src/seulseul/slack/handlers.py` | Slack 이벤트·명령·버튼이 서비스로 들어가는 입구 |
| 3 | `src/seulseul/notices/service.py` | 공지 검증, 저장, 수정·삭제, AI 실패·재처리 규칙 |
| 4 | `src/seulseul/ai/service.py` / `ai/client.py` | 프롬프트, JSON 파싱·근거 검증, Ollama/NVIDIA 호출 |
| 5 | `src/seulseul/checklists/service.py` / `checklists/repository.py` | 학생별 목록, 같은 DM 갱신, 버튼 처리와 lease |
| 6 | `src/seulseul/slack/client.py` / `slack/views.py` | Slack Web API 호출과 Block Kit 화면 |
| 7 | `src/seulseul/jobs/scheduler.py` / `jobs/tasks.py` | 주기 실행·변경 신호·재시도 연결 |
| 8 | `src/seulseul/config.py` / `database.py` / 각 `model.py` | 환경변수, 세션, 영속 데이터 구조 |
| 9 | `src/seulseul/oauth_server.py` / `compose.prod.yaml` | 외부 워크스페이스 설치와 공개 HTTPS 경로 |

## 3. 전체 구조

~~~~mermaid
flowchart LR
    Slack["Slack 워크스페이스"] --> Socket["Socket Mode / Bolt"]
    Socket --> Handlers["slack/handlers.py"]
    Handlers --> Services["도메인 service.py"]
    Services --> Repositories["repository.py"]
    Repositories --> DB[("PostgreSQL")]
    Services --> Clients["Slack·AI client.py"]
    Clients --> Slack
    Clients --> AI["Ollama 또는 NVIDIA"]
    Scheduler["jobs/scheduler.py"] --> Services
    Services -. 변경 신호 .-> Scheduler

    Browser["설치 브라우저"] --> Ngrok["ngrok HTTPS"]
    Ngrok --> OAuth["oauth_server.py"]
    OAuth --> OAuthStore[("OAuth 전용 볼륨")]
    OAuth --> Slack
~~~~

구조도 원본은 [seulseul-architecture.svg](diagrams/seulseul-architecture.svg)입니다. 문서에서 Mermaid가 렌더링되지 않는 환경에서는 SVG를 엽니다.

## 4. 핵심 흐름

### 4.1 공지 수집 → AI 분석 → 체크리스트 반영

~~~~mermaid
flowchart TD
    A["Slack message 이벤트"] --> B["handlers.py<br/>create_message_event_handler"]
    B --> C["NoticeService.parse_event"]
    C --> D{"최상위 메시지·허용 채널·링크인가?"}
    D -- "아니오" --> X["무시<br/>DB 변경 없음"]
    D -- "예" --> E["record_channel_message"]
    E --> F["원본 버전 잠금·URL 정규화<br/>posted_at을 Asia/Seoul로 변환"]
    F --> G["NoticeAnalyzer.analyze"]
    G --> H["ai/client.py<br/>JSON 요청"]
    H --> I["ai/service.py<br/>필드·근거·날짜·시간 검증"]
    I --> J{"분석 성공?"}
    J -- "예" --> K["NoticeRepository에 processed 저장"]
    J -- "아니오" --> L["processing_failed 저장<br/>일시 오류만 자동 재시도 예약"]
    K --> M["wakeup 신호"]
    M --> N["ChecklistScheduler 즉시 실행"]
    N --> O["학생별 DM 동기화"]
    L --> P["5분·15분·1시간<br/>최대 3회 자동 재시도"]
    P --> G
~~~~

핵심은 **AI 호출 결과를 그대로 믿지 않는 것**입니다. `ai/service.py`가 공지 원문 안의 날짜·시간 근거, `deadline_source_text`, `Asia/Seoul` 오프셋, 게시 시각 이후 여부를 확인한 뒤에만 `processed`로 저장합니다. 타임테이블의 수업 시각처럼 마감과 무관한 시각은 별도 규칙으로 제외합니다.

### 4.2 가입 → 학생별 DM → 버튼 갱신

~~~~mermaid
flowchart TD
    A["/seulseul 시작"] --> B["slack/handlers.py<br/>ack() 후 StudentService"]
    B --> C["Slack 성명·반 검증"]
    C --> D["학생 저장·변경 신호"]
    D --> E["DailyChecklistService"]
    E --> F["ChecklistRepository.prepare_delivery<br/>대상 공지·완료 상태·현재 DM 선택"]
    F --> G{"기존 DM이 있는가?"}
    G -- "아니오" --> H["SlackChecklistClient.chat_postMessage"]
    G -- "예" --> I{"board_hash가 바뀌었는가?"}
    I -- "예" --> J["같은 메시지 chat_update"]
    I -- "아니오" --> K["Slack 호출 생략"]
    H --> L["lease로 발송 주소 저장"]
    J --> L
    K --> L
    L --> M["체크리스트 표시 완료"]
    N["완료·취소 버튼"] --> O["handler가 ack()"]
    O --> P["DailyChecklistService.handle_action"]
    P --> Q["Repository가 사용자·DM·항목을 검증"]
    Q --> R["완료 상태 저장 + 같은 DM 갱신"]
~~~~

새 공지나 프로필 변경은 `wakeup`으로 즉시 동기화를 깨웁니다. 일반 동기화는 날짜별 새 DM을 만드는 기능이 아니며, 학생별 현재 DM 하나를 유지하는 것이 기본 규칙입니다.

### 4.3 AI 응답 검증과 실패 처리

~~~~mermaid
flowchart LR
    A["Slack 게시 시각 + 원문 + 링크"] --> B["프롬프트"]
    B --> C["Ollama / NVIDIA<br/>thinking 비활성화"]
    C --> D["JSON 응답"]
    D --> E{"필수 필드·JSON인가?"}
    E -- "아니오" --> F["실패 원인 저장"]
    E -- "예" --> G{"원문에 근거가 있고<br/>날짜·시간이 하나로 확정되는가?"}
    G -- "아니오" --> F
    G -- "예" --> H{"게시 시각보다 미래인가?"}
    H -- "아니오" --> F
    H -- "예" --> I["processed 분석값 저장"]
    F --> J{"일시적 오류인가?"}
    J -- "예" --> K["자동 재시도 대기열"]
    J -- "아니오" --> L["admin.sh retry 또는 notice로<br/>운영자 수동 처리"]
~~~~

AI 요청 형식은 `ai/client.py`, 결과의 의미 검증은 `ai/service.py`에 있습니다. 모델을 바꿀 때는 요청 형식보다 **파싱·근거 검증과 실제 공지 테스트**를 먼저 확인합니다.

## 5. 파일 책임 지도

| 영역 | 파일 | 책임 | 수정할 때의 기준 |
| --- | --- | --- | --- |
| 조립 | `main.py` | 설정 로드, 저장소·서비스·스케줄러 연결 | 새 의존성을 연결할 때만 수정 |
| Slack 입구 | `slack/handlers.py` | 이벤트 검증, `ack()`, 서비스 호출, 응답 | 업무 규칙·DB 쿼리를 넣지 않음 |
| Slack API | `slack/client.py` | Web API·DM 전송·메시지 갱신·삭제 | Slack 호출은 이 경계 안에 둠 |
| 화면 | `slack/views.py` | Block Kit과 표시 문자열 | 화면 문구 변경 시 `BOARD_PRESENTATION_VERSION` 확인 |
| 공지 | `notices/service.py` | 링크·채널·원본 버전·대상·AI 상태 | 수정·삭제 경합과 재시도 규칙의 중심 |
| 공지 DB | `notices/repository.py`, `notices/model.py` | 원본·공지 조회/저장·이벤트 적용 | 트랜잭션·잠금 영향 확인 |
| AI 호출 | `ai/client.py` | Ollama/NVIDIA HTTP 요청, timeout, 재시도 | 비밀값·원문을 로그에 남기지 않음 |
| AI 규칙 | `ai/service.py`, `ai/deadline.py` | 프롬프트, JSON/날짜/근거 검증 | 시간대와 원문 근거 테스트 추가 |
| 체크리스트 | `checklists/service.py` | 학생별 목록·동기화·버튼 업무 규칙 | Slack 호출은 서비스 밖 client로 위임 |
| 체크리스트 DB | `checklists/repository.py`, `checklists/model.py` | 배정·완료 상태·DM lease/hash | 학생·메시지 소유권 검증 유지 |
| 학생 | `users/service.py`, `users/repository.py` | 가입·해지·프로필·반 변경 | 성명 오류 시 기존 데이터를 지움 금지 |
| 반복 작업 | `jobs/tasks.py`, `jobs/scheduler.py` | 공지 재시도·체크리스트 갱신 | 업무 규칙을 scheduler에 복제하지 않음 |
| 설정/DB | `config.py`, `database.py` | 환경변수 검증·세션/엔진 | 다른 모듈에서 `os.getenv()` 사용 금지 |
| OAuth | `oauth_server.py`, `oauth_wsgi.py` | `/healthz`, `/slack/install`, callback | 공개 경로는 OAuth만, 설치 파일은 전용 볼륨 |

## 6. 바꾸려는 내용별 시작점

| 바꾸려는 것 | 먼저 열 파일 | 함께 확인할 것 |
| --- | --- | --- |
| 어떤 Slack 메시지를 공지로 볼지 | `notices/events.py`, `notices/service.py` | `PRODUCT.md`, 공지 서비스 테스트 |
| AI 프롬프트/모델/timeout | `ai/client.py`, `ai/service.py`, `config.py` | JSON 스키마, 실패·재시도 테스트 |
| 마감일 해석 | `ai/deadline.py`, `ai/service.py` | `Asia/Seoul`, 게시일, 타임테이블 분리 |
| 공지 수정·삭제 | `notices/events.py`, `notices/service.py`, `notices/repository.py` | `notice_sources`, 완료 상태 보존 |
| 체크리스트 문구/버튼 | `slack/views.py`, `checklists/service.py` | `BOARD_PRESENTATION_VERSION`, 같은 DM 갱신 |
| 완료 버튼 동작 | `slack/handlers.py`, `checklists/service.py` | `checklists/repository.py`의 메시지 주소 검증 |
| 가입·반 판별 | `users/service.py`, `slack/handlers.py` | `users.info`, `user_change`, 개인정보 로그 |
| 자동 재시도 | `notices/service.py`, `jobs/tasks.py` | `retry_count`, `next_retry_at`, 운영 CLI |
| OAuth 설치 | `oauth_server.py`, `config.py`, `compose.prod.yaml` | `SLACK_REDIRECT_URI`, ngrok 도메인, 저장 볼륨 |
| 운영 명령 | `admin.sh`(관리 콘솔), `view.sh`, `run.sh`, `stop.sh` | 운영 환경에서만 실제 컨테이너 검증 |

## 7. 꼭 지켜야 하는 불변조건

1. **경계**: 외부 Slack·AI 호출은 각 `client.py`에만 둡니다. 핸들러는 `ack()`와 입력 검증 후 서비스를 호출합니다.
2. **시간**: 공지 게시 시각·마감일·로그 표시를 `Asia/Seoul` 기준으로 다룹니다. `오늘`, `금일`, 시각 없는 마감의 해석은 `PRODUCT.md` 정책을 따릅니다.
3. **AI 근거**: AI가 날짜를 말했더라도 원문에서 마감 표현과 날짜·시각 근거가 확인되지 않으면 성공 처리하지 않습니다.
4. **DM 중복 방지**: 현재 학생의 발송 기록, DM 채널, 메시지 `ts`, `board_hash`를 함께 검증합니다. 불명확한 Slack 전송 결과를 재발송하지 않습니다.
5. **수정·삭제**: 공지 원본 버전이 바뀌는 동안 예전 AI 결과가 새 원문을 덮어쓰지 못해야 합니다. 완료 상태는 공지 수정만으로 초기화하지 않습니다.
6. **보안**: `.env`, 토큰, 실제 사용자 정보, 원문 개인정보를 커밋하거나 로그에 남기지 않습니다.

## 8. 로컬 검증과 운영 확인

코드 변경 후 기본 검증은 저장소 루트에서 실행합니다.

~~~~bash
./scripts/check.sh
~~~~

운영 서버 반영은 자동 pull이 아닙니다. 백업이 필요한 상태라면 먼저 백업한 뒤 서버에서 다음 순서로 실행합니다.

~~~~bash
git pull --ff-only origin main
./run.sh
./view.sh status
./view.sh logs --follow
~~~~

AI 실패 공지는 다음 명령으로 목록과 수동 재처리를 확인합니다.

~~~~bash
./admin.sh retry
./admin.sh retry N
~~~~

OAuth가 필요할 때는 `./view.sh status`, `curl http://127.0.0.1:<oauth-port>/healthz`, `./view.sh logs oauth`, `./view.sh logs ngrok` 순서로 확인합니다. 실제 포트는 서버 `.env`의 `SLACK_OAUTH_PORT`를 기준으로 합니다.

## 9. 관련 문서

- [제품 정책](PRODUCT.md)
- [모듈 경계와 데이터 흐름](ARCHITECTURE.md)
- [운영자 명령](OPERATIONS.md)
- [DB 접근과 SSH 터널](DB-ACCESS.md)
- [결정 기록](DECISIONS.md)
- [운영·보안 작업 계획](PLANS.md)
