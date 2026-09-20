# 로컬·서버 작업 목록

PostgreSQL과 Docker Compose의 환경별 작업·운영 기록입니다. 현재 잔여 작업은 `docs/PLANS.md`, 과거 구현 이력은 `docs/HISTORY.md`, 결정은 `docs/DECISIONS.md`를 따릅니다. 아래의 2026-09-15 상태는 당시 기록이며 현재 서버 직접 조회 결과가 아닙니다. 사용자 확인과 운영 직접 검증을 구분합니다.

공개 저장소이므로 서버 IP, 접속 주소, 비밀번호, 계정 정보, 같은 서버의 다른 서비스 정보는 적지 않습니다.

## 전제

- DB: PostgreSQL, SQLAlchemy, Alembic (D-016)
- 환경 분리: 로컬은 `compose.yaml`로 PostgreSQL만 실행하고 Python 앱은 호스트에서 실행합니다. 운영 서버는 `compose.prod.yaml`로 봇과 PostgreSQL을 함께 실행합니다.
- 로컬·운영은 PostgreSQL 메이저 버전과 Alembic 마이그레이션만 공유하고, DB 이름·계정·비밀번호·볼륨은 분리합니다.
- 운영 AI: NVIDIA Build API (D-017). **서버에서는 Ollama를 실행하지 않습니다.** Ollama는 로컬 개발용입니다.
- Slack: Socket Mode (D-018). 서버에 들어오는 포트를 열지 않습니다.
- 서버: 이미 생성된 Oracle Cloud VM 한 대. 배포 계정 `seulseul`의 **Rootless Docker**로 Docker Compose를 실행합니다.
- 메모리: 서버에서 SeulSeul이 쓰는 메모리는 **모든 컨테이너를 합쳐 최대 6GB**이며, `seulseul` 사용자 단위로 OS가 강제합니다.
- 동일 Slack 앱의 봇은 로컬·서버를 합쳐 **한 번에 1개만** 실행합니다. 여러 개를 띄우면 이벤트가 나뉘고 메시지 정리·갱신이 경합합니다. 일일 정시 DM은 보내지 않습니다.

## 서버 현재 상태 (2026-09-15 기준)

| 항목 | 값 |
| --- | --- |
| OS | Ubuntu 24.04.5 LTS (20.04 → 22.04 → 24.04 업그레이드) |
| CPU 구조 | `aarch64` (ARM), CPU 1개 |
| 메모리 | 전체 17GiB, 기본 사용 약 0.8GiB, 스왑 없음 |
| 디스크 | 45GB 중 약 21GB 여유 |
| 시간대 | `Asia/Seoul`, NTP 동기화 |
| 일반 Docker (root 권한) | Engine 29.8.0, Compose 5.5.1 설치. **`docker`·`docker.socket`·`containerd` 서비스 꺼짐(disabled)** |
| Rootless Docker | `seulseul` 전용. 부팅 시 자동 시작(linger), 데이터 `~/.local/share/docker`, 설정 `~/.config/docker/daemon.json`(로그 `json-file` `10m × 3`) |
| 방화벽 | `iptables-nft` + `iptables-persistent`, fail2ban `sshd`·`sshd-seulseul` |
| 메모리 강제 제한 | `user-1002.slice`(`seulseul`) `MemoryMax=6G` |

## 배포 계정 `seulseul`

| 항목 | 설정 |
| --- | --- |
| 그룹 | `seulseul`, `users`, `seulseul-deploy` (`docker`·`sudo` 그룹 없음) |
| 관리자 권한 | 없음. OS 업데이트·방화벽·서비스 관리는 `ubuntu` 계정으로 합니다. |
| Docker | 자기 전용 Rootless Docker만 사용. 일반 Docker 소켓 접근 차단 확인 |
| 파일 위치 | `/home/seulseul` 안에만 둡니다. 저장소는 `~/app`, DB 백업은 `~/backups`(700) |
| 홈 폴더 권한 | 750 |
| SSH 로그인 | `seulseul-deploy` 그룹만 비밀번호 로그인 허용. 다른 계정은 키 로그인만 |
| SSH 부가 기능 | `seulseul-deploy` 그룹은 TCP·소켓 포워딩, 에이전트 전달, X11, 터널 금지 |
| 메모리 | 이 계정의 모든 프로세스·컨테이너 합계 최대 6GB |

- 접속은 SSH로 직접 로그인합니다. `sudo -iu seulseul`처럼 계정을 전환하면 Rootless Docker가 동작하지 않을 수 있습니다.
- 위 SSH 그룹 정책은 당시 상태입니다. 이후 15432·기존 비밀번호 SSH 터널 안내 후 DBeaver 접속 성공이 확인됐으므로 실제 계정·포워딩 적용값은 재확인합니다. 현재 접속과 전용 계정 선택안은 `docs/DB-ACCESS.md`를 따릅니다.
- 비밀번호는 비밀번호 관리자에서 무작위로 생성해 보관하고, 채팅·문서·저장소에 적지 않습니다.
- 설정 파일: `/etc/ssh/sshd_config.d/90-seulseul-password.conf` (원본 백업 `/root/90-seulseul-password.conf.bak`)

## 서버 운영 규칙

- **`sudo netfilter-persistent save`를 실행하지 않습니다.** 현재 적용된 규칙을 그대로 저장해 fail2ban 차단 목록이 규칙 파일에 섞입니다. 방화벽을 바꿀 때는 `/etc/iptables/rules.v4`를 직접 수정하고 `sudo -n sh -c 'iptables-restore --test < /etc/iptables/rules.v4'`로 시험한 뒤 적용합니다.
- **규칙 파일에는 fail2ban 규칙(`f2b-…`)을 넣지 않습니다.** fail2ban이 시작할 때 스스로 추가합니다.
- **컨테이너 포트를 외부 인터페이스에 공개하지 않습니다.** PostgreSQL은 루프백 바인딩·SSH 터널로만 접근합니다. 저장소 5432와 접속 성공 안내 15432의 차이는 재배포 전 확인합니다. `0.0.0.0`, `[::]`, IP 생략 바인딩과 DB 포트 방화벽 개방은 금지합니다. SSH의 허용 목적지는 실제 루프백 DB 포트로 제한하며 적용값을 확인합니다.
- **일반 Docker 서비스를 다시 켜지 않습니다.** 필요하면 이유를 `docs/DECISIONS.md`에 기록한 뒤 켭니다.
- **`/etc/default/netfilter-persistent`는 Oracle 이미지 설정(`IPTABLES_RESTORE_NOFLUSH=yes`)을 유지합니다.**
- `ubuntu` 계정의 관리 명령은 `sudo -n`으로 실행합니다. `sudo` 없이 `systemctl restart` 등을 실행하면 비밀번호를 물은 뒤 실패합니다.
- 여러 줄 명령은 한 줄씩 실행합니다. 비밀번호를 묻는 명령이 섞이면 뒤따르는 줄이 입력으로 소비됩니다.

### fail2ban 기준

| jail | 대상 | 기준 |
| --- | --- | --- |
| `sshd` | `seulseul`을 제외한 SSH 로그인 실패 | 10시간 안에 2회 실패 → 60일 차단 (`/etc/fail2ban/jail.local` 기본값) |
| `sshd-seulseul` | `seulseul` 계정의 SSH 로그인 실패 | 1시간 안에 5회 실패 → 1일 차단 |

- 설정 파일: `/etc/fail2ban/jail.d/zz-seulseul.local`, `/etc/fail2ban/filter.d/sshd-seulseul.conf`. 기존 `jail.local`은 수정하지 않았습니다.
- fail2ban은 **IP를 차단**합니다. `seulseul` 기준으로 차단돼도 같은 IP에서는 `ubuntu` 접속까지 막힙니다.
- 차단 해제: `ubuntu` 계정으로 `sudo -n fail2ban-client set sshd-seulseul unbanip <IP>` (다른 IP나 Oracle 콘솔에서 접속)

### 2026-09-15 방화벽 복원 경위

- 20.04 → 22.04 업그레이드 중 `/etc/iptables/rules.v4`가 거의 빈 규칙으로 덮어써져, 재부팅 후 기존 허용·차단 규칙이 적용되지 않았습니다. 방화벽 방식이 legacy에서 nft로 바뀌는 과정에서 생긴 것으로 추정합니다(원인 미확인).
- 업그레이드 전 기록에서 fail2ban 규칙만 뺀 규칙으로 파일을 복원했고, 이후 재부팅에서 INPUT 규칙 22개가 유지되는 것을 확인했습니다.
- 서버의 `/root/rules.v4.broken`(덮어써진 파일)과 `~/upgrade-before/`(업그레이드 전 서비스·포트·방화벽 기록)는 당분간 보관합니다.

## 0. 먼저 합의하고 기록할 것

- [x] DB를 PostgreSQL로 확정 (D-016)
- [x] ORM·마이그레이션 도구: SQLAlchemy, Alembic (D-016)
- [x] 운영 AI 제공자와 초기 모델 (D-017)
- [x] 배포 방식 기록: 기존 Oracle ARM VM(Ubuntu 24.04) + Rootless Docker Compose, 서버 Ollama 미사용 (D-028)
- [ ] 배포 계정 권한 구조(Rootless Docker, 비밀번호 로그인 그룹, fail2ban 계정별 기준) 결정 기록
- [ ] NVIDIA로 전송할 공지 데이터와 개인정보 처리 정책 (`docs/PLANS.md`)
- [x] DB 드라이버 선택: Psycopg 3.3 binary + SQLAlchemy 2.0 동기 엔진 (D-020)

## 1. 로컬

- D-027 마이그레이션 `d94132ac684e`는 로컬 격리 PostgreSQL에서 backfill·upgrade/downgrade·위험 downgrade 거절을 검증했고 테스트 트랜잭션은 롤백했습니다. 이후 사용자가 운영 반별 동일 링크 흐름을 확인했습니다. 운영 revision의 직접 조회 기록은 별도로 확인하며 향후 마이그레이션은 README의 백업·`run.sh` 절차를 따릅니다.

### 1-1. PostgreSQL 기반

의존성 추가, 연결·세션, 모델·첫 마이그레이션의 완료 이력은 `docs/HISTORY.md`를 참고합니다. 여기서는 Docker 관련 작업만 다룹니다.

- [x] `compose.yaml`에 로컬 개발용 `postgres` 서비스만 추가. 포트는 `127.0.0.1:5432`에만 연결하고 개발 전용 named volume 사용
- [x] `.env.example`의 로컬 `DATABASE_URL` 형식 주석 채우기 (값은 비움, 호스트는 `127.0.0.1`)
- [x] DB 테스트 방식 결정: 단위 테스트는 실제 DB 없이 실행하고, 마이그레이션·통합 테스트는 로컬 개발 DB만 사용. 서버 DB는 테스트에 쓰지 않음

### 1-2. 로컬 앱 실행

- [x] `docker compose up -d postgres`로 DB만 실행하고 Python 앱은 프로젝트 `.venv`에서 실행
- [ ] 로컬 앱이 `127.0.0.1:5432`의 개발 DB에 연결하고 Alembic 마이그레이션을 적용하는지 확인
- [ ] 로컬 Ollama 사용 시 `OLLAMA_BASE_URL=http://localhost:11434/v1`로 연결

### 1-3. 메모리 측정

- [ ] 공지 수신·AI 분석·명령어 응답을 몇 번 실행하면서 호스트의 Python 봇 프로세스와 `docker stats`의 `postgres` 메모리 최대치 기록
- [ ] 측정값을 바탕으로 2-4의 제한값 확정 (측정값 + 여유 50% 정도, 합계 6GB 이하)

## 2. 서버 (Oracle Cloud)

### 운영 간편 명령

루트의 `run.sh`, `stop.sh`, `view.sh`를 사용합니다. 상세 옵션과 최초 설치·업데이트 순서는 `README.md`의 "운영 서버 간편 명령"을 따릅니다. `run.sh`는 빌드 후 봇 중지·DB 준비·마이그레이션·봇 재생성 순서이고, `stop.sh`는 DB 볼륨을 삭제하지 않습니다. Docker 데몬 설치·설정과 방화벽 변경은 수행하지 않습니다. 최초 적용 후 서버에서 실행·중지·로그 조회를 확인해야 합니다.

### 2-1. 서버 확인과 OS

- [x] 사양 확인: `aarch64`, CPU 1개, 메모리 17GiB, 디스크 45GB
- [x] 같은 서버의 다른 프로그램 메모리 확인: SeulSeul이 6GB를 써도 여유 충분
- [x] Ubuntu 20.04 → 24.04.5 LTS 업그레이드 (Docker 공식 지원 대상이 22.04 이상)
- [x] 업그레이드 후 서비스·방화벽 규칙 복원과 재부팅 후 유지 확인
- [x] netdata 삭제 (Netdata Cloud 용도로 판단)
- [x] SSH 전체 기본값이 비밀번호 로그인 금지인지 확인 (`60-cloudimg-settings.conf`)
- [ ] Oracle 보안 목록(클라우드 방화벽)에서 들어오는 포트 확인. SeulSeul용으로 추가로 열 포트는 없음
- [ ] 스왑 추가 결정 (현재 0B. 1~2GB 권장)
- [ ] 유휴 인스턴스 회수 정책 확인 (D-007 미확인 항목). 봇과 DB는 CPU를 거의 쓰지 않아 대상이 될 수 있음

### 2-2. Docker와 배포 계정

- [x] 일반 Docker Engine·Compose 설치 (공식 apt 저장소, `noble`, `arm64`)
- [x] 일반 Docker 서비스 끄기 (`docker`, `docker.socket`, `containerd` disabled, 재부팅 후 inactive 확인)
- [x] 배포 계정 `seulseul` 생성, `sudo`·`docker` 그룹 없음 확인
- [x] `seulseul` Rootless Docker 설치: `uidmap`, AppArmor `rootlesskit` 프로필, linger, 자동 시작
- [x] Rootless Docker 확인: 데이터 홈 폴더 저장, `rootless` 보안 옵션, 컨테이너 외부 통신, 일반 Docker 접근 차단, 재부팅 후 자동 시작
- [x] `seulseul` 메모리 6GB 제한 적용, 재부팅 후 유지 확인
- [x] `seulseul-deploy` 그룹 생성·추가
- [x] fail2ban `sshd-seulseul` jail 적용 (재부팅 후 `sshd`, `sshd-seulseul` 동작 확인)
- [x] `/opt/seulseul` 삭제
- [x] `~/app`, `~/backups` 폴더 존재 (사용자 터미널 출력 확인, 현재 권한은 별도 검증)
- [ ] SSH 그룹 규칙 적용값과 목적지 한정 포워딩 확인 (`docs/DB-ACCESS.md`)
- [ ] fail2ban 기준값 확인: `sshd-seulseul` `5 / 3600 / 86400`, `sshd` `2 / 36000 / 5184000`

### 2-3. 운영 이미지와 배포 (`seulseul` 계정)

- [x] 하위 패키지 `__init__.py` 추가 (D-001 결과 항목. 없으면 이미지 설치본에서 하위 패키지가 빠짐)
- [x] `config.py`의 `.env` 경로 전제 정리. 컨테이너에서는 Compose가 환경변수를 넣음
- [x] `Dockerfile`: `python:3.11-slim` 기반, 컨테이너 안에서도 비루트 사용자로 실행, 의존성 설치 레이어 분리
- [x] `.dockerignore`: `.env`, `.venv/`, 캐시, `.git/` 제외
- [x] `compose.prod.yaml`에 `bot`·`postgres` 추가, 루프백 DB 포트·운영 named volume·healthcheck 사용
- [x] 운영 DB는 로컬과 다른 DB 이름·계정·비밀번호를 사용하고 `DATABASE_URL`의 호스트는 Compose 서비스명 `postgres`로 설정
- [x] 서버와 같은 `linux/arm64` 이미지 빌드 확인 (`docker build --platform linux/arm64`)
- [x] 서버 Rootless Docker Compose에서 운영 흐름 확인 (사용자 확인)
- [x] `~/app` 저장소·서버 환경 설정 후 운영 시작 (사용자 확인, `.env` 파일 권한·비밀값은 직접 검사하지 않음)
- [x] 마이그레이션 후 운영 봇 시작 (사용자 확인, 최신 revision 직접 조회는 잔여 작업)
- [ ] 봇 컨테이너가 1개만 실행 중인지 확인
- [ ] `restart: unless-stopped` 설정 완료. VM 재부팅 후 자동 시작은 서버 배포 시 확인
- [x] 업데이트 절차는 README의 운영 명령만 사용: 서버 변경 확인 → 코드 갱신 → 백업 성공 → `./run.sh`. 실행 중인 봇을 둔 채 마이그레이션하지 않습니다.

### 2-4. 메모리 제한 (합계 최대 6GB)

`seulseul` 사용자 전체는 OS가 6GB로 제한합니다. 그 안에서 서비스끼리 메모리를 나누도록 Compose에도 서비스별 제한을 둡니다. 아래는 초기값이며 1-3 측정 후 6GB 안에서 조정합니다.

| 서비스 | 메모리 제한 (초기값) | 설정 |
| --- | --- | --- |
| `bot` | 512MB | 봇 1개. AI는 외부 API라 모델을 메모리에 올리지 않음 |
| `postgres` | 1GB | `shared_buffers=256MB`, `effective_cache_size=768MB`, `work_mem=4MB`, `maintenance_work_mem=64MB`, `max_connections=20` |
| 마이그레이션 컨테이너 | 512MB | 실행할 때만 사용 |
| `ollama` | 실행 안 함 | 서버에서는 사용하지 않음 |
| **합계** | **2GB** | 상한 6GB 중 남는 4GB는 측정 결과에 따라 늘릴 여유 |

- [x] `compose.prod.yaml`에 서비스별 메모리 제한 반영, 합계 6GB 이하인지 확인
- `pg_dump`·아카이브 검사는 postgres 컨테이너 내부에서 실행하므로 DB의 1GB 제한을 공유합니다. 별도 512MB 백업 컨테이너는 없습니다.
- [ ] 배포 후 하루 정도 `docker stats`로 사용량 확인. 제한에 가까우면 합계 6GB 안에서 늘림

### 2-5. 운영

- [x] 수동 운영 백업 `./backup.sh`: custom-format pg_dump, private 파일 권한, 전체 해독 검사·SHA-256, 실패 시 부분 파일 정리 및 이전 백업 보존 (실제 서버 실행·복구 시험은 별도)

- [x] 자동 백업 등록·해제 스크립트 `backup_run.sh`·`backup_stop.sh`: 한국 시간 매일 03시, systemd 사용자 타이머·lingering, 중단 기간 보충 실행. 상세 사용법은 README의 운영 DB 백업 참고
- [ ] 운영 서버에서 자동 백업 등록 후 실제 성공 로그·파일 확인
- [ ] VM 밖(예: Oracle Object Storage) 보관 및 보존 기간 정책 확정·적용 (현재 자동 삭제 없음)
- [ ] 백업 복구 시험 1회
- [x] 로그 확인 방법 정리: README의 `./view.sh logs`, 백업 journal 확인
- [x] 실제 워크스페이스의 주요 학생 흐름 점검 (사용자 확인, 참여 인원·세부 실행 증거는 별도 기록)

### 2-6. 업그레이드 후 남은 정리

- [ ] 남은 패키지 업데이트 적용 (`sudo -n apt upgrade`)
- [ ] netdata 패키지가 없는 것을 확인한 뒤 남은 라이브러리 정리 (`sudo -n apt autoremove`)
- [ ] 업그레이드 전 Oracle 부트 볼륨 백업 생성 여부 확인, 며칠 안정 운영 후 보관·삭제 결정 (무료 한도 확인)
- [ ] 서버의 `~/upgrade-before/`, `/root/rules.v4.broken`, `/root/90-seulseul-password.conf.bak` 삭제 시점 결정
