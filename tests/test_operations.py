"""가짜 Docker로 운영 셸의 인자·실행 순서·안전한 실패를 검증한다."""

import hashlib
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_production_database_port_is_only_bound_to_ipv4_loopback():
    config = (ROOT / "compose.prod.yaml").read_text()
    assert config.count("    ports:\n") == 2
    assert '    ports:\n      - "127.0.0.1:15432:5432"\n' in config
    assert '    ports:\n      - "127.0.0.1:5432:5432"\n' not in config
    assert '      - "127.0.0.1:${SLACK_OAUTH_PORT:-8080}:8080"\n' in config
    assert "image: ngrok/ngrok:3.39.11-debian" in config
    assert "NGROK_AUTHTOKEN" in config
    assert "NGROK_DOMAIN" in config
    assert "oauth:8080" in config
    assert "service_healthy" in config
    assert '      - "0.0.0.0' not in config and '      - "[::]' not in config
    assert "postgres_prod_data:/var/lib/postgresql" in config


@pytest.fixture
def operations(tmp_path, monkeypatch):
    repo = tmp_path / "app with spaces"
    repo.mkdir()
    (repo / "scripts").mkdir()
    for name in (
        "run.sh",
        "stop.sh",
        "view.sh",
        "admin.sh",
        "scripts/operations.sh",
        "scripts/ui.sh",
        "scripts/menu.sh",
        "scripts/console.sh",
        "scripts/notice.sh",
        "scripts/retry.sh",
        "scripts/announce.sh",
        "scripts/backup.sh",
        ".env.example",
    ):
        shutil.copyfile(ROOT / name, repo / name)
    (repo / ".env").write_text("SECRET=never_print_this\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> docker-calls
if [[ "$1" == info ]]; then
    if [[ -f nonroot ]]; then echo '[]'; else echo '["name=rootless"]'; fi
fi
if [[ -f fail-step ]]; then
    read -r fail < fail-step
    if [[ "$*" == *"$fail"* ]]; then exit 17; fi
fi
if [[ "$*" == *'exec pg_dump '* ]]; then
    if [[ ! -f empty-dump ]]; then printf 'fake-custom-archive'; fi
fi
if [[ "$*" == *'pg_restore --file=/dev/null'* ]]; then
    cat >/dev/null
fi
if [[ -f emit-logs && "$*" == *' logs '* ]]; then
    cat emit-logs
fi
if [[ -f emit-ps && "$*" == *' ps -a --format '* ]]; then
    cat emit-ps
fi
if [[ -f emit-output && "$*" == *' run --rm '* ]]; then
    cat emit-output
fi
if [[ -f emit-stderr && "$*" == *manual_console* ]]; then
    printf 'console screen\n'
    cat emit-stderr >&2
fi
"""
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries), prepend=":")

    def run(script, *args, input_text=None):
        return subprocess.run(
            ["bash", str(repo / script), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=10,
            check=False,
        )

    def calls():
        path = repo / "docker-calls"
        if not path.exists():
            return []
        return path.read_text().splitlines()

    return repo, run, calls


def test_announcement_runs_only_one_shot_tool(operations):
    _, run, calls = operations
    result = run("admin.sh", "announce")
    assert result.returncode == 0
    assert calls()[-1].endswith(
        "run --rm --no-deps --interactive --tty bot python -m seulseul.users.announce"
    )
    assert not any("up -d" in call or "exec" in call for call in calls())


def test_announcement_help_needs_no_environment(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()
    assert run("admin.sh", "announce", "--help").returncode == 0
    assert calls() == []


def test_run_builds_stops_migrates_then_recreates_single_bot(operations):
    _, run, calls = operations
    result = run("run.sh")
    assert result.returncode == 0, result.stderr
    steps = calls()
    expected = [
        "config --quiet",
        "build bot migrate oauth",
        "stop bot oauth ngrok",
        "up -d --wait --wait-timeout 180 postgres",
        "run --rm migrate",
        "up -d --no-deps --force-recreate --scale bot=1 bot oauth ngrok",
        "ps -a --format {{.Service}}|{{.State}}|{{.Status}}",
    ]
    assert len(steps) == len(expected) + 2
    for actual, suffix in zip(steps[2:], expected, strict=True):
        assert "compose.prod.yaml" in actual
        assert actual.endswith(suffix)
    assert "never_print_this" not in result.stdout + result.stderr


def test_notice_runs_unified_console_without_starting_another_bot(operations):
    _, run, calls = operations
    result = run("admin.sh", "notice", "--limit", "50", "--workspace-id", "TTEST")
    assert result.returncode == 0, result.stderr
    assert calls()[-1].endswith(
        "run --rm --no-deps -T bot python -m seulseul.notices.manual_console "
        "--limit 50 --workspace-id TTEST"
    )
    assert not any(
        "seulseul.main" in call or " up " in call or " stop " in call for call in calls()
    )
    assert "never_print_this" not in result.stdout + result.stderr


def test_retry_help_explains_number_selection_without_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()

    result = run("admin.sh", "retry", "--help")

    assert result.returncode == 0
    assert "./admin.sh retry 3" in result.stdout
    assert "./view.sh dashboard" in result.stdout
    assert calls() == []


def test_admin_help_explains_the_shortcuts_without_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()

    result = run("admin.sh", "--help")

    assert result.returncode == 0
    assert "./view.sh dashboard" in result.stdout
    assert "./admin.sh retry N" in result.stdout
    assert "./admin.sh notice" in result.stdout
    assert "./admin.sh backup schedule" in result.stdout
    assert "\x1b[" not in result.stdout
    assert calls() == []


def test_admin_opens_read_only_dashboard_and_exits_from_menu(operations):
    _, run, calls = operations

    result = run("admin.sh", input_text="q\n")

    assert result.returncode == 0, result.stderr
    assert any(
        call.endswith("run --rm --no-deps -T bot python -m seulseul.notices.dashboard --panel")
        for call in calls()
    )
    assert "관리자 메뉴를 종료합니다." in result.stdout


def test_notice_help_does_not_require_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()
    result = run("admin.sh", "notice", "--help")
    assert result.returncode == 0 and "등록" in result.stdout
    assert calls() == []


def test_notice_propagates_cli_failure_without_starting_another_bot(operations):
    repo, run, calls = operations
    (repo / "fail-step").write_text("seulseul.notices.manual_console\n")
    assert run("admin.sh", "notice").returncode == 17
    assert not any(" up " in call or "build " in call for call in calls())


@pytest.mark.parametrize("invalid", ["nonroot", "missing_env"])
def test_notice_requires_safe_operations_setup(operations, invalid):
    repo, run, calls = operations
    if invalid == "nonroot":
        (repo / "nonroot").touch()
    else:
        (repo / ".env").unlink()
    assert run("admin.sh", "notice").returncode != 0
    assert not any(" run " in call for call in calls())


@pytest.mark.parametrize(
    "failure", ["build bot migrate oauth", "run --rm migrate", "config --quiet"]
)
def test_run_failure_never_starts_bot(operations, failure):
    repo, run, calls = operations
    (repo / "fail-step").write_text(failure + "\n")
    assert run("run.sh").returncode == 17
    assert not any("--force-recreate" in call for call in calls())
    if failure != "run --rm migrate":
        assert not any(call.endswith("stop bot oauth ngrok") for call in calls())


@pytest.mark.parametrize("operation", ["all", "bot", "oauth", "ngrok", "postgres"])
def test_stop_preserves_data_and_stops_bot_before_database(operations, operation):
    _, run, calls = operations
    assert run("stop.sh", operation).returncode == 0
    steps = calls()
    if operation == "bot":
        assert steps[3].endswith("stop bot")
    elif operation == "oauth":
        assert steps[3].endswith("stop ngrok oauth")
    elif operation == "ngrok":
        assert steps[3].endswith("stop ngrok")
    elif operation == "postgres":
        assert steps[3].endswith("stop bot oauth ngrok postgres")
    else:
        assert steps[3].endswith("stop bot oauth ngrok")
    if operation == "all":
        assert steps[4].endswith("stop postgres")
    assert not any(" down" in call or "volume" in call for call in steps)


def test_setup_never_overwrites_existing_env_and_creates_private_template(operations):
    repo, run, calls = operations
    original = (repo / ".env").read_text()
    assert run("run.sh", "setup").returncode == 0
    assert (repo / ".env").read_text() == original
    (repo / ".env").unlink()
    assert run("run.sh", "setup").returncode == 0
    assert (repo / ".env").stat().st_mode & 0o777 == 0o600
    assert (repo / ".env").read_text() == (repo / ".env.example").read_text()
    assert calls() == []


@pytest.mark.parametrize(
    "script,args",
    [
        ("run.sh", ["unknown"]),
        ("stop.sh", ["all", "extra"]),
        ("view.sh", ["logs", "--bad"]),
        ("view.sh", ["status", "extra"]),
        ("admin.sh", ["unknown"]),
        ("admin.sh", ["help", "extra"]),
        ("admin.sh", ["retry", "abc"]),
        ("admin.sh", ["retry", "0"]),
        ("admin.sh", ["backup", "now", "extra"]),
    ],
)
def test_invalid_arguments_never_call_docker(operations, script, args):
    _, run, calls = operations
    assert run(script, *args).returncode == 2
    assert calls() == []


@pytest.mark.parametrize("command", ["status", "logs", "db", "schema", "config"])
def test_views_do_not_start_or_stop_services(operations, command):
    _, run, calls = operations
    assert run("view.sh", command).returncode == 0
    steps = calls()
    assert not any(" up " in call or " stop " in call or " run " in call for call in steps)
    if command in {"db", "schema"}:
        assert "default_transaction_read_only=on" in steps[-1]
        assert '"$POSTGRES_USER"' in steps[-1]


def test_view_accepts_ngrok_logs(operations):
    _, run, calls = operations
    result = run("view.sh", "logs", "ngrok")
    assert result.returncode == 0
    assert calls()[-1].endswith("logs --no-color --tail 100 ngrok")


def test_view_dashboard_runs_read_only_dashboard_cli(operations):
    _, run, calls = operations
    result = run("view.sh", "dashboard")

    assert result.returncode == 0, result.stderr
    assert calls()[-1].endswith("run --rm --no-deps -T bot python -m seulseul.notices.dashboard")
    assert not any(" up " in call or " stop " in call for call in calls())


def test_view_formats_human_activity_without_internal_identifiers(operations):
    repo, run, _ = operations
    (repo / "emit-logs").write_text(
        "\n".join(
            [
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:46:31,339 INFO main.py:handle_message_event: "
                    "메시지 이벤트 제외: channel=C0BE3PB2STC ts=1790055990.976559"
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:52:55,354 INFO main.py:handle_checklist_action: "
                    '{"diagnostic_version": 1, "event": "checklist_action_received", '
                    '"trace_id": "trace-hidden", "actor_name": "홍길동", "operation": "refresh"}'
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:52:55,354 INFO seulseul.checklists.service: "
                    '{"diagnostic_version": 1, "event": "checklist_action_parsed", '
                    '"trace_id": "trace-hidden"}'
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:52:55,360 INFO seulseul.checklists.repository: "
                    '{"diagnostic_version": 1, "event": "checklist_message_validation", '
                    '"trace_id": "trace-hidden", "result": "payload_match", '
                    '"reason": "match"}'
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:52:55,376 INFO seulseul.checklists.service: "
                    '{"diagnostic_version": 1, "event": "checklist_action_result", '
                    '"trace_id": "trace-hidden", "saved": true, '
                    '"dm_synchronized": true}'
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:53:33,932 ERROR seulseul.checklists.service: "
                    '{"diagnostic_version": 1, "event": "checklist_action_error", '
                    '"trace_id": "error-hidden", "actor_name": "김철수", '
                    '"operation": "complete"}'
                ),
                (
                    "seulseul-prod-bot-1  | "
                    "2026-09-22 05:54:00,000 INFO main.py:handle_seulseul_command: "
                    "명령어 처리: actor=홍길동 action=시작 result=가입 완료"
                ),
            ]
        )
        + "\n"
    )

    result = run("view.sh", "logs", "bot")

    assert result.returncode == 0, result.stderr
    assert "시각 | 상태 | 주체 | 작업 결과" in result.stdout
    assert "Slack | 수집 대상이 아닌 메시지를 무시했습니다" in result.stdout
    assert "홍길동 | 체크리스트 새로고침 완료 · 개인 DM 갱신 완료" in result.stdout
    assert "김철수 | 공지 완료 처리 실패 · 운영자 확인 필요" in result.stdout
    assert "trace-hidden" not in result.stdout
    assert "C0BE3PB2STC" not in result.stdout
    assert "1790055990.976559" not in result.stdout
    assert "event=" not in result.stdout
    assert "payload_match" not in result.stdout
    assert "홍길동 | /seulseul 시작 · 가입 완료" in result.stdout

    follow_result = run("view.sh", "logs", "bot", "--follow")

    assert follow_result.returncode == 0, follow_result.stderr
    assert "최근 로그 + 실시간 추적" in follow_result.stdout
    assert "Ctrl+C" in follow_result.stdout

    raw_result = run("view.sh", "logs", "bot", "--raw")

    assert raw_result.returncode == 0, raw_result.stderr
    assert "Docker 원본 로그" in raw_result.stdout
    assert "channel=C0BE3PB2STC" in raw_result.stdout
    assert "trace-hidden" in raw_result.stdout


def test_missing_env_and_nonrootless_docker_fail_before_mutation(operations):
    repo, run, calls = operations
    (repo / "nonroot").touch()
    assert run("run.sh").returncode != 0
    assert not any(" up " in call for call in calls())
    (repo / ".env").unlink()
    count = len(calls())
    assert run("run.sh").returncode != 0
    assert len(calls()) == count


@pytest.mark.parametrize("operation", ["postgres", "migrate"])
def test_partial_run_does_not_start_bot(operations, operation):
    _, run, calls = operations
    assert run("run.sh", operation).returncode == 0
    assert not any("--force-recreate" in call for call in calls())
    assert any(call.endswith("run --rm migrate") for call in calls()) == (operation == "migrate")


def test_backup_creates_private_verified_archive_without_stopping_services(operations):
    repo, run, calls = operations
    result = run("admin.sh", "backup")
    assert result.returncode == 0, result.stderr
    destination = repo.parent / "backups"
    archives = list(destination.glob("*/database.dump"))
    assert len(archives) == 1 and archives[0].read_bytes() == b"fake-custom-archive"
    assert destination.stat().st_mode & 0o777 == 0o700
    assert archives[0].stat().st_mode & 0o777 == 0o600
    assert (archives[0].parent / "SHA256SUMS").is_file()
    assert (archives[0].parent / "SHA256SUMS").read_text().strip() == (
        hashlib.sha256(archives[0].read_bytes()).hexdigest() + "  database.dump"
    )
    assert not list(destination.glob("*/*.partial"))
    steps = calls()
    assert "exec pg_dump" in steps[-2]
    assert "-T postgres" in steps[-2]
    assert steps[-1].endswith("pg_restore --file=/dev/null")
    assert not any(" stop " in line or " down" in line or " run " in line for line in steps)
    assert "never_print_this" not in result.stdout + result.stderr
    assert run("admin.sh", "backup").returncode == 0
    assert len(list(destination.glob("*/database.dump"))) == 2


@pytest.mark.parametrize("failure", ["exec pg_dump", "pg_restore --file=/dev/null", "empty"])
def test_backup_failure_removes_only_current_partial_files(operations, failure):
    repo, run, _ = operations
    destination = repo.parent / "backups"
    destination.mkdir()
    previous = destination / "previous.dump"
    previous.write_text("keep")
    if failure == "empty":
        (repo / "empty-dump").touch()
    else:
        (repo / "fail-step").write_text(failure + "\n")
    result = run("admin.sh", "backup")
    assert result.returncode != 0
    assert "백업 완료:" not in result.stdout
    assert list(destination.iterdir()) == [previous]
    assert previous.read_text() == "keep"


def test_backup_rejects_symlink_destination_and_unknown_option(operations):
    repo, run, calls = operations
    (repo.parent / "backups").symlink_to(repo, target_is_directory=True)
    assert run("admin.sh", "backup").returncode != 0
    assert not any("exec pg_dump" in line for line in calls())
    count = len(calls())
    assert run("admin.sh", "backup", "--delete").returncode == 2
    assert run("admin.sh", "backup", "--help").returncode == 0
    assert len(calls()) == count


@pytest.fixture
def scheduling(operations, monkeypatch):
    repo, run, calls = operations
    config = repo.parent / "user-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    binaries = repo.parent / "bin"
    for name, body in {
        "systemctl": """printf '%s\\n' "$*" >> systemctl-calls
if [[ -f fail-systemctl && "$*" == *"$(cat fail-systemctl)"* ]]; then exit 19; fi
if [[ "$*" == *is-enabled* && -f "$XDG_CONFIG_HOME/systemd/user/seulseul-db-backup.timer" ]]; then
    echo enabled
fi
""",
        "loginctl": "if [[ -f no-linger ]]; then echo no; else echo yes; fi\n",
    }.items():
        binary = binaries / name
        binary.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        binary.chmod(0o700)
    return repo, run, calls, config / "systemd/user"


def test_schedule_is_daily_korean_time_private_and_repeatable(scheduling):
    repo, run, calls, units = scheduling
    for _ in range(2):
        result = run("admin.sh", "backup", "schedule")
        assert result.returncode == 0, result.stderr
    service = units / "seulseul-db-backup.service"
    timer = units / "seulseul-db-backup.timer"
    assert service.stat().st_mode & 0o777 == 0o600
    assert timer.stat().st_mode & 0o777 == 0o600
    # 예약 서비스는 인자 없이 실행하므로 scripts/backup.sh의 무인자 실행이 곧 백업이어야 한다.
    assert 'ExecStart=/bin/bash "' + str(repo / "scripts/backup.sh") + '"\n' in service.read_text()
    assert 'Environment="DOCKER_HOST=unix://%t/docker.sock"' in service.read_text()
    assert 'Environment="DOCKER_CONTEXT="' in service.read_text()
    assert "TimeoutStartSec=30min" in service.read_text()
    assert "TimeoutStopSec=30s" in service.read_text()
    assert "TimeoutStartSec=infinity" not in service.read_text()
    assert "OnCalendar=*-*-* 03:00:00 Asia/Seoul" in timer.read_text()
    assert "Persistent=true" in timer.read_text()
    assert sorted(p.name for p in units.iterdir()) == [service.name, timer.name]
    commands = (repo / "systemctl-calls").read_text()
    assert commands.count("--user enable seulseul-db-backup.timer\n") == 2
    assert commands.count("--user restart seulseul-db-backup.timer\n") == 2
    assert not any("exec pg_dump" in line or " up " in line for line in calls())


def test_unschedule_disables_only_timer_even_without_database_configuration(scheduling):
    repo, run, calls, units = scheduling
    assert run("admin.sh", "backup", "schedule").returncode == 0
    (repo / ".env").unlink()
    (repo / "no-linger").touch()
    before = calls()
    for _ in range(2):
        assert run("admin.sh", "backup", "unschedule").returncode == 0
    assert calls() == before
    commands = (repo / "systemctl-calls").read_text()
    assert "--user disable --now seulseul-db-backup.timer" in commands
    assert "stop seulseul-db-backup.service" not in commands
    assert (units / "seulseul-db-backup.service").exists()


def test_schedule_requires_linger_and_preserves_foreign_units(scheduling):
    repo, run, _, units = scheduling
    (repo / "no-linger").touch()
    result = run("admin.sh", "backup", "schedule")
    assert result.returncode != 0
    assert "enable-linger" in result.stderr
    assert not units.exists()
    (repo / "no-linger").unlink()
    units.mkdir(parents=True)
    timer = units / "seulseul-db-backup.timer"
    timer.write_text("# someone else's timer\n")
    for action in ("schedule", "unschedule"):
        assert run("admin.sh", "backup", action).returncode != 0
        assert timer.read_text() == "# someone else's timer\n"
    timer.unlink()
    timer.symlink_to(repo / ".env")
    assert run("admin.sh", "backup", "schedule").returncode != 0
    assert (repo / ".env").read_text() == "SECRET=never_print_this\n"


def test_schedule_errors_do_not_report_success(scheduling):
    repo, run, _, _ = scheduling
    (repo / "fail-systemctl").write_text("enable")
    result = run("admin.sh", "backup", "schedule")
    assert result.returncode == 19
    assert "등록 완료" not in result.stdout


def test_schedule_help_invalid_arguments_and_absent_unschedule(scheduling):
    repo, run, calls, _ = scheduling
    help_result = run("admin.sh", "backup", "--help")
    assert help_result.returncode == 0
    assert "schedule" in help_result.stdout and "unschedule" in help_result.stdout
    for action in ("schedule", "unschedule"):
        assert run("admin.sh", "backup", action, "--delete").returncode == 2
    assert calls() == []
    assert not (repo / "systemctl-calls").exists()
    assert run("admin.sh", "backup", "unschedule").returncode == 0


def test_schedule_escapes_systemd_specifiers_and_variables(scheduling):
    repo, _, _, units = scheduling
    renamed = repo.with_name('app % $ "quoted"')
    repo.rename(renamed)
    result = subprocess.run(
        ["bash", str(renamed / "admin.sh"), "backup", "schedule"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    service = (units / "seulseul-db-backup.service").read_text()
    assert 'app %% $$ \\"quoted\\"/scripts/backup.sh' in service


def test_scheduled_backup_entrypoint_without_arguments_creates_archive(operations):
    repo, run, calls = operations
    result = run("scripts/backup.sh")
    assert result.returncode == 0, result.stderr
    assert len(list((repo.parent / "backups").glob("*/database.dump"))) == 1
    assert "백업 완료:" in result.stdout
    assert steps_end_with_restore_check(calls())


def steps_end_with_restore_check(steps):
    return "exec pg_dump" in steps[-2] and steps[-1].endswith("pg_restore --file=/dev/null")


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], "python -m seulseul.notices.retry list --limit 100"),
        (["3"], "python -m seulseul.notices.retry retry --index 3 --limit 100"),
        (["pending"], "python -m seulseul.notices.retry pending --limit 100"),
        (
            ["retry", "--workspace-id", "TTEST", "--url", "https://forms.example.test/a"],
            "python -m seulseul.notices.retry retry --limit 100 "
            "--workspace-id TTEST --url https://forms.example.test/a",
        ),
    ],
)
def test_admin_retry_uses_dashboard_numbering_limit(operations, args, expected):
    _, run, calls = operations
    result = run("admin.sh", "retry", *args)
    assert result.returncode == 0, result.stderr
    assert calls()[-1].endswith("run --rm --no-deps -T bot " + expected)


def test_admin_retry_propagates_cli_failure(operations):
    repo, run, _ = operations
    (repo / "fail-step").write_text("seulseul.notices.retry\n")
    assert run("admin.sh", "retry", "1").returncode == 17


def test_status_shows_services_in_plain_words(operations):
    repo, run, _ = operations
    (repo / "emit-ps").write_text(
        "bot|running|Up 2 hours\npostgres|running|Up 2 hours (healthy)\nngrok|exited|Exited (0)\n"
    )
    result = run("view.sh")
    assert result.returncode == 0, result.stderr
    assert "● bot" in result.stdout and "실행 중 · Slack 봇" in result.stdout
    assert "○ ngrok" in result.stdout and "중지됨" in result.stdout
    assert "./view.sh dashboard" in result.stdout


def test_status_explains_when_no_container_exists(operations):
    _, run, _ = operations
    result = run("view.sh", "status")
    assert result.returncode == 0
    assert "./run.sh" in result.stdout


def test_run_failure_names_the_failed_step(operations):
    repo, run, _ = operations
    (repo / "fail-step").write_text("run --rm migrate\n")
    result = run("run.sh")
    assert result.returncode == 17
    assert "[4/5] DB 마이그레이션 단계에서 실패했습니다" in result.stderr
    assert "자동으로 다시 켜지 않았습니다" in result.stderr


def test_admin_menu_backup_requires_confirmation(operations):
    _, run, calls = operations
    cancelled = run("admin.sh", input_text="7\n\n\nq\n")
    assert cancelled.returncode == 0, cancelled.stderr
    assert not any("exec pg_dump" in call for call in calls())
    confirmed = run("admin.sh", input_text="7\ny\n\nq\n")
    assert confirmed.returncode == 0, confirmed.stderr
    assert any("exec pg_dump" in call for call in calls())
    assert "백업 완료:" in confirmed.stdout


def test_admin_menu_retry_confirms_selected_number(operations):
    _, run, calls = operations
    result = run("admin.sh", input_text="1\n2\ny\n\nq\n")
    assert result.returncode == 0, result.stderr
    assert any(
        call.endswith("seulseul.notices.retry retry --index 2 --limit 100") for call in calls()
    )
    invalid = run("admin.sh", input_text="1\nabc\n\nq\n")
    assert invalid.returncode == 0
    assert "1 이상의 숫자" in invalid.stdout


def test_admin_menu_survives_failed_tool_and_returns_to_menu(operations):
    repo, run, _ = operations
    (repo / "fail-step").write_text("seulseul.notices.manual_console\n")
    result = run("admin.sh", input_text="2\n\nq\n")
    assert result.returncode == 0, result.stderr
    assert "작업이 끝나지 않았거나 실패했습니다" in result.stdout
    assert "관리자 메뉴를 종료합니다." in result.stdout


def test_backup_status_reads_only_and_guides_next_step(scheduling):
    repo, run, calls, _ = scheduling
    empty = run("admin.sh", "backup", "status")
    assert empty.returncode == 0, empty.stderr
    assert "꺼짐" in empty.stdout and "./admin.sh backup schedule" in empty.stdout
    assert "아직 성공한 백업이 없습니다" in empty.stdout
    assert calls() == []
    assert run("admin.sh", "backup").returncode == 0
    assert run("admin.sh", "backup", "schedule").returncode == 0
    status = run("admin.sh", "backup", "status")
    assert "켜짐 · 매일 03:00" in status.stdout
    assert "마지막 성공" in status.stdout and "보관 1개" in status.stdout


def test_legacy_backup_schedule_is_reported_until_registered_again(scheduling):
    repo, run, _, units = scheduling
    units.mkdir(parents=True)
    (units / "seulseul-db-backup.service").write_text(
        "# Managed by SeulSeul backup scheduling scripts\n"
        '[Service]\nExecStart=/bin/bash "' + str(repo / "backup.sh") + '"\n'
    )
    (units / "seulseul-db-backup.timer").write_text(
        "# Managed by SeulSeul backup scheduling scripts\n[Timer]\n"
    )
    warned = run("admin.sh", "retry")
    assert warned.returncode == 0
    assert "예약 백업이 실패합니다" in warned.stderr
    assert "./admin.sh backup schedule" in warned.stderr
    assert "예약 백업이 실패합니다" in run("admin.sh", "backup", "status").stderr
    assert run("admin.sh", "backup", "schedule").returncode == 0
    assert "예약 백업이 실패합니다" not in run("admin.sh", "retry").stderr


class Terminal:
    """가상 터미널에서 관리 콘솔을 실행한다. 출력을 계속 비워 화면 쓰기가 막히지 않게 한다."""

    def __init__(self, repo, *args, rows=40, cols=120):
        pty = pytest.importorskip("pty")
        import fcntl
        import struct
        import termios

        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def attach():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        # 환경변수는 config 밖에서 읽지 않으므로 env 명령으로 터미널 종류만 지정한다.
        self.process = subprocess.Popen(
            ["env", "TERM=xterm-256color", "bash", str(repo / "admin.sh"), *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=repo.parent,
            preexec_fn=attach,
        )
        self.terminal_settings = os.dup(slave)
        os.close(slave)
        self.output = b""

    def _drain(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.output += chunk

    def expect(self, text, *, count=1, timeout=10):
        needle = text.encode()
        deadline = time.monotonic() + timeout
        while self.output.count(needle) < count:
            if time.monotonic() > deadline:
                screen = self.output.decode(errors="replace")[-3000:]
                raise AssertionError(f"{text!r} 미표시:\n{screen}")
            self._drain(0.1)

    def send(self, keys):
        os.write(self.master, keys.encode())

    def finish(self, timeout=10):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self._drain(0.1)
        if self.process.poll() is None:
            self.process.kill()
            raise AssertionError("콘솔이 종료되지 않았습니다.")
        os.close(self.master)
        os.close(self.terminal_settings)
        return self.process.returncode

    def line_mode(self):
        """입력 표시(echo)와 줄 단위 입력(icanon)이 켜져 있는지. 콘솔이 살아 있을 때만 읽을 수 있다.

        macOS는 세션을 연 프로세스가 끝나면 터미널을 회수하므로 종료 뒤에는 확인하지 않는다.
        복원은 작업 실행과 종료가 같은 leave_screen을 쓰므로 작업 실행 중 상태로 검증한다.
        """
        import termios

        local_flags = termios.tcgetattr(self.terminal_settings)[3]
        return bool(local_flags & termios.ECHO) and bool(local_flags & termios.ICANON)


def test_console_opens_full_screen_and_restores_terminal(operations):
    repo, _, _ = operations
    terminal = Terminal(repo)
    terminal.expect("슬슬 관리 콘솔")
    terminal.expect("실패한 공지 AI로 다시 분석")
    terminal.expect("서비스 시작·재배포")
    terminal.expect("? 도움말")
    terminal.send("?")
    terminal.expect("공지 상태 용어")
    terminal.send("x")
    terminal.expect("슬슬 관리 콘솔", count=2)
    terminal.send("q")
    assert terminal.finish() == 0
    assert b"\x1b[?1049h" in terminal.output
    assert b"\x1b[?1049l" in terminal.output


def test_console_number_key_asks_before_backup(operations):
    repo, _, calls = operations
    terminal = Terminal(repo)
    terminal.expect("슬슬 관리 콘솔")
    assert not terminal.line_mode()
    terminal.send("7")
    terminal.expect("지금 운영 DB를 백업할까요?")
    assert terminal.line_mode()
    terminal.send("\n")
    terminal.expect("취소했습니다.")
    terminal.expect("Enter를 누르면 돌아갑니다")
    terminal.send("\n")
    terminal.expect("슬슬 관리 콘솔", count=2)
    assert not terminal.line_mode()
    assert not any("exec pg_dump" in call for call in calls())
    terminal.send("7")
    terminal.expect("지금 운영 DB를 백업할까요?", count=2)
    terminal.send("y\n")
    terminal.expect("백업 완료:")
    terminal.expect("Enter를 누르면 돌아갑니다", count=2)
    terminal.send("\n")
    terminal.expect("슬슬 관리 콘솔", count=3)
    terminal.send("q")
    assert terminal.finish() == 0
    assert any("exec pg_dump" in call for call in calls())


def test_console_arrow_keys_select_and_enter_runs(operations):
    repo, _, calls = operations
    terminal = Terminal(repo)
    terminal.expect("슬슬 관리 콘솔")
    terminal.send("\x1b[B")
    terminal.expect("봇이 읽지 못하는 채널의 공지를 등록하거나")
    terminal.send("\n")
    terminal.expect("Enter를 누르면 돌아갑니다")
    assert any("seulseul.notices.manual_console --limit 100" in call for call in calls())
    terminal.send("\n")
    terminal.expect("슬슬 관리 콘솔", count=2)
    terminal.send("q")
    assert terminal.finish() == 0


@pytest.mark.parametrize("args,size", [((), (20, 70)), (("--simple",), (40, 120))])
def test_small_window_or_simple_flag_uses_line_menu(operations, args, size):
    repo, _, _ = operations
    terminal = Terminal(repo, *args, rows=size[0], cols=size[1])
    terminal.expect("무엇을 할까요? 번호를 입력하세요")
    terminal.send("q\n")
    assert terminal.finish() == 0
    assert b"\x1b[?1049h" not in terminal.output


def test_console_ctrl_c_restores_terminal(operations):
    repo, _, _ = operations
    terminal = Terminal(repo)
    terminal.expect("슬슬 관리 콘솔")
    terminal.send("\x03")
    assert terminal.finish() == 130
    assert b"\x1b[?25h" in terminal.output and b"\x1b[?1049l" in terminal.output


def test_console_failed_action_returns_to_console(operations):
    repo, _, _ = operations
    (repo / "fail-step").write_text("seulseul.notices.manual_console\n")
    terminal = Terminal(repo)
    terminal.expect("슬슬 관리 콘솔")
    terminal.send("2")
    terminal.expect("작업이 끝나지 않았거나 실패했습니다")
    terminal.expect("Enter를 누르면 돌아갑니다")
    terminal.send("\n")
    terminal.expect("슬슬 관리 콘솔", count=2)
    terminal.send("q")
    assert terminal.finish() == 0


def test_console_limits_panel_width_without_refetching(operations):
    repo, _, calls = operations
    terminal = Terminal(repo, cols=250)
    terminal.expect("슬슬 관리 콘솔")
    terminal._drain(3)
    terminal.send("q")
    assert terminal.finish() == 0
    fetches = [call for call in calls() if "seulseul.notices.dashboard" in call]
    assert len(fetches) == 1
    assert fetches[0].endswith("--panel --width 240")


def test_console_reports_unreadable_service_state(operations):
    repo, _, _ = operations
    (repo / "fail-step").write_text("ps -a --format\n")
    terminal = Terminal(repo)
    terminal.expect("서비스 상태를 읽지 못했습니다")
    terminal.send("q")
    assert terminal.finish() == 0
    assert "실행 중인 서비스가 없습니다".encode() not in terminal.output


@pytest.mark.parametrize("backup_fails", [True, False])
def test_menu_redeploy_requires_successful_backup_when_requested(operations, backup_fails):
    repo, run, calls = operations
    if backup_fails:
        (repo / "fail-step").write_text("exec pg_dump\n")
    result = run("admin.sh", "--simple", input_text="9\ny\ny\n\nq\n")
    assert result.returncode == 0, result.stderr
    built = any(call.endswith("build bot migrate oauth") for call in calls())
    assert built is not backup_fails
    if backup_fails:
        assert "재배포를 시작하지 않았습니다" in result.stderr
    else:
        dump_index = next(i for i, call in enumerate(calls()) if "exec pg_dump" in call)
        build_index = next(
            i for i, call in enumerate(calls()) if call.endswith("build bot migrate oauth")
        )
        assert dump_index < build_index


def test_schedule_from_moved_repository_is_reported_as_stale(scheduling):
    repo, run, _, units = scheduling
    units.mkdir(parents=True)
    (units / "seulseul-db-backup.service").write_text(
        "# Managed by SeulSeul backup scheduling scripts\n"
        '[Service]\nExecStart=/bin/bash "/old/place/app/scripts/backup.sh"\n'
    )
    (units / "seulseul-db-backup.timer").write_text(
        "# Managed by SeulSeul backup scheduling scripts\n[Timer]\n"
    )
    status = run("admin.sh", "backup", "status")
    assert status.returncode == 0
    assert "켜짐" not in status.stdout
    assert "scripts/backup.sh가 아닌 경로" in status.stderr
    assert run("admin.sh", "backup", "schedule").returncode == 0
    assert "켜짐 · 매일 03:00" in run("admin.sh", "backup", "status").stdout


def test_notice_keeps_screen_and_appends_console_logs_to_private_file(operations):
    repo, run, _ = operations
    (repo / "emit-stderr").write_text(
        "2026-10-07 14:32:05,123 ERROR seulseul.notices.manual_console: "
        "수동 공지 처리 실패: action=등록 stage=원문 링크 확인 error=ValueError "
        "reason=Slack 원문 링크 형식이 올바르지 않습니다.\n"
    )
    for _ in range(2):
        result = run("admin.sh", "notice")
        assert result.returncode == 0, result.stderr
    assert "console screen" in result.stdout
    assert "수동 공지 처리 실패" in result.stderr
    log_dir = repo.parent / "logs"
    log_file = log_dir / "manual-notice.log"
    assert log_file.read_text().count("수동 공지 처리 실패") == 2
    assert "console screen" not in log_file.read_text()
    assert log_dir.stat().st_mode & 0o777 == 0o700
    assert log_file.stat().st_mode & 0o777 == 0o600


def test_notice_refuses_symlinked_log_file(operations):
    repo, run, calls = operations
    log_dir = repo.parent / "logs"
    log_dir.mkdir()
    (log_dir / "manual-notice.log").symlink_to(repo / ".env")
    result = run("admin.sh", "notice")
    assert result.returncode == 1
    assert "심볼릭 링크" in result.stderr
    assert not any("manual_console" in call for call in calls())
    assert (repo / ".env").read_text() == "SECRET=never_print_this\n"


def test_view_manual_logs_are_readable_and_raw_is_available(operations):
    repo, run, _ = operations
    missing = run("view.sh", "logs", "manual")
    assert missing.returncode == 0
    assert "아직 수동 공지 기록이 없습니다" in missing.stdout
    log_dir = repo.parent / "logs"
    log_dir.mkdir()
    (log_dir / "manual-notice.log").write_text(
        "2026-10-07 14:32:05,123 ERROR seulseul.notices.manual_console: 수동 공지 처리 실패: "
        "action=등록 stage=원문 링크 확인 error=ValueError reason=Slack 원문 링크 형식이 "
        "올바르지 않습니다.\n"
        "2026-10-07 14:35:00,000 INFO seulseul.notices.manual_console: 수동 공지 처리 완료: "
        "action=등록 target=2반 links=1 analysis=수동\n"
    )
    result = run("view.sh", "logs", "manual")
    assert result.returncode == 0, result.stderr
    assert (
        "오류 | 운영자 | 수동 공지 등록 실패 · 원문 링크 확인 · Slack 원문 링크 형식이 올바르지 "
        "않습니다." in result.stdout
    )
    assert "정상 | 운영자 | 수동 공지 등록 완료 · 배정 2반" in result.stdout
    raw = run("view.sh", "logs", "manual", "--raw")
    assert "error=ValueError" in raw.stdout
