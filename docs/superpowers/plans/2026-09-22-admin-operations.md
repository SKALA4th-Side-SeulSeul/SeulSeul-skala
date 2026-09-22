# 관리자 운영 도구 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 운영자가 공지 상태를 한 화면에서 확인하고, 번호 선택만으로 AI 재처리·수동 공지 관리를 실행할 수 있는 운영 도구와 사용 문서를 제공한다.

**Architecture:** `view.sh dashboard`는 기존 PostgreSQL 공지 상태를 조회하는 읽기 전용 Python CLI를 실행하고, `admin.sh`는 대시보드의 번호와 기존 `retry.sh`·`notice.sh`를 연결하는 얇은 운영 메뉴로 동작한다. `retry.sh retry --index N`은 DB에서 실패 공지를 다시 선택해 workspace·URL·채널·ts 복사를 없애며, 기존 공지 서비스와 재처리의 동시성·완료 기록 보존 규칙을 재사용한다.

**Tech Stack:** Bash, Python 3.11, SQLAlchemy repository/service, pytest, Docker Compose.

**Spec:** 운영자용 사용 설명서 `docs/OPERATIONS.md`.

## Global Constraints

- DB 스키마와 마이그레이션을 추가하지 않는다.
- `config.py` 밖에서 환경변수를 직접 읽지 않는다.
- 운영 CLI는 기존 `NoticeService`와 repository를 통해서만 공지를 조회·변경한다.
- `retry.sh`는 AI 재호출이며, 잘못된 AI 결과는 `notice.sh`의 수동 분석 입력으로 수정한다.
- 운영 Compose는 두 번째 Socket Mode 봇을 시작하지 않는다.
- 기본 화면에는 내부 식별자와 비밀값을 표시하지 않는다.
- 관리자 작업은 저장 전 한 번 확인하고, `q`·EOF·Ctrl+C는 안전하게 취소한다.

## Review Focus

- 실패 목록의 번호와 `retry --index` 선택 순서가 항상 동일해야 한다.
- 자동 재시도 예약 중인 항목은 수동 조치 대상으로 잘못 표시하지 않아야 한다.
- 실패 목록이 비어 있거나 선택 번호가 범위를 벗어나도 DB·AI 호출 없이 종료해야 한다.
- 대시보드와 관리자 메뉴가 Rootless Docker·운영 `.env` 검증을 통과하기 전 컨테이너를 실행하지 않아야 한다.
- 기존 직접 명령과 수동 공지 수정 흐름은 그대로 동작해야 한다.

### Task 1: Dashboard read model and output

**Files:**
- Create: `src/seulseul/notices/dashboard.py`
- Test: `tests/test_notices_dashboard.py`

**Interfaces:**
- Produces `render_dashboard(failed, pending_sources, recent, now) -> str`.
- Produces CLI `python -m seulseul.notices.dashboard` for `view.sh`.

- [x] Write failing tests for automatic-retry/manual-action grouping and human-readable output.
- [x] Run the focused tests and confirm the missing module failure.
- [x] Implement the read-only dashboard formatter and CLI using existing `NoticeService` queries.
- [x] Run the focused tests and confirm the output is stable.

### Task 2: Number-based retry selection

**Files:**
- Modify: `src/seulseul/notices/retry.py`
- Modify: `retry.sh`
- Test: `tests/test_notices.py`
- Test: `tests/test_operations.py`

**Interfaces:**
- Adds `retry --index N [--limit N] [--workspace-id ID]`.
- Keeps explicit `--workspace-id --url [--channel-id --message-ts]` compatible.

- [x] Write failing tests for selecting a failed notice by number and rejecting an invalid number without AI calls.
- [x] Run the focused tests and confirm failure.
- [x] Implement index selection from the same ordered `failed_notices` list used by the dashboard.
- [x] Update shell help and run focused tests.

### Task 3: Interactive administrator menu

**Files:**
- Create: `admin.sh`
- Modify: `tests/test_operations.py`

**Interfaces:**
- `./admin.sh` opens the dashboard menu.
- The menu launches `./retry.sh retry --index`, `./notice.sh`, `./retry.sh pending`, and `./view.sh logs bot --follow`.

- [x] Write shell tests for help, safe setup validation, and command forwarding.
- [x] Run the focused shell tests and confirm the new command is absent/fails.
- [x] Implement the menu with one confirmation before AI retry and no direct DB manipulation.
- [x] Run focused shell tests and verify existing operations remain unchanged.

### Task 4: Administrator guide and repository tracking

**Files:**
- Create: `docs/OPERATIONS.md`
- Modify: `README.md`
- Modify: `docs/PLANS.md`

- [x] Write the guide around daily monitoring, failure triage, manual correction, and exact commands.
- [x] Add the guide to the README command table and clarify `retry.sh` is AI retry.
- [x] Update the active plan item.
- [x] Run documentation link and whitespace checks.

### Task 5: Full verification

- [x] Run focused tests after every task.
- [x] Run `bash -n` on new and changed shell scripts.
- [x] Run the complete Python 3.11 test, Ruff, format, and compile suite in Docker.
- [x] Review `git diff`, exclude unrelated `scripts/operations.sh` mode changes, and commit with the repository format.
