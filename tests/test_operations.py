"""가짜 Docker로 운영 셸의 인자·실행 순서·안전한 실패를 검증한다."""

import hashlib
import shutil
import subprocess
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
        "backup.sh",
        "backup_run.sh",
        "backup_stop.sh",
        "notice.sh",
        "retry.sh",
        "admin.sh",
        "notice_edit.sh",
        "scripts/scheduling.sh",
        "scripts/operations.sh",
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
        "ps -a",
    ]
    assert len(steps) == len(expected) + 2
    for actual, suffix in zip(steps[2:], expected, strict=True):
        assert "compose.prod.yaml" in actual
        assert actual.endswith(suffix)
    assert "never_print_this" not in result.stdout + result.stderr


def test_notice_edit_runs_only_interactive_cli_and_forwards_arguments(operations):
    _, run, calls = operations
    result = run("notice_edit.sh", "--limit", "50", "--workspace-id", "TTEST")
    assert result.returncode == 0, result.stderr
    assert calls()[-1].endswith(
        "run --rm --no-deps -T bot python -m seulseul.notices.manual_edit "
        "--limit 50 --workspace-id TTEST"
    )
    assert not any(
        "seulseul.main" in call or " up " in call or " stop " in call for call in calls()
    )
    assert "never_print_this" not in result.stdout + result.stderr


def test_notice_runs_unified_console_without_starting_another_bot(operations):
    _, run, calls = operations
    result = run("notice.sh", "--limit", "50", "--workspace-id", "TTEST")
    assert result.returncode == 0, result.stderr
    assert calls()[-1].endswith(
        "run --rm --no-deps -T bot python -m seulseul.notices.manual_console "
        "--limit 50 --workspace-id TTEST"
    )
    assert not any(
        "seulseul.main" in call or " up " in call or " stop " in call for call in calls()
    )


def test_retry_help_explains_number_selection_without_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()

    result = run("retry.sh", "--help")

    assert result.returncode == 0
    assert "--index N" in result.stdout
    assert calls() == []


def test_admin_help_explains_the_shortcuts_without_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()

    result = run("admin.sh", "--help")

    assert result.returncode == 0
    assert "./view.sh dashboard" in result.stdout
    assert "./retry.sh retry --index N" in result.stdout
    assert "./notice.sh" in result.stdout
    assert calls() == []


def test_admin_opens_read_only_dashboard_and_exits_from_menu(operations):
    _, run, calls = operations

    result = run("admin.sh", input_text="q\n")

    assert result.returncode == 0, result.stderr
    assert any(
        call.endswith("run --rm --no-deps -T bot python -m seulseul.notices.dashboard")
        for call in calls()
    )
    assert "관리자 메뉴를 종료합니다." in result.stdout


def test_notice_help_does_not_require_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()
    result = run("notice.sh", "--help")
    assert result.returncode == 0 and "등록" in result.stdout
    assert calls() == []


def test_notice_edit_help_does_not_require_docker_or_env(operations):
    repo, run, calls = operations
    (repo / ".env").unlink()
    result = run("notice_edit.sh", "--help")
    assert result.returncode == 0 and "번호" in result.stdout
    assert calls() == []


def test_notice_edit_propagates_cli_failure_without_starting_another_bot(operations):
    repo, run, calls = operations
    (repo / "fail-step").write_text("seulseul.notices.manual_edit\n")
    assert run("notice_edit.sh").returncode == 17
    assert not any(" up " in call or "build " in call for call in calls())


@pytest.mark.parametrize("invalid", ["nonroot", "missing_env"])
def test_notice_edit_requires_safe_operations_setup(operations, invalid):
    repo, run, calls = operations
    if invalid == "nonroot":
        (repo / "nonroot").touch()
    else:
        (repo / ".env").unlink()
    assert run("notice_edit.sh").returncode != 0
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
    result = run("backup.sh")
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
    assert run("backup.sh").returncode == 0
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
    result = run("backup.sh")
    assert result.returncode != 0
    assert "백업 완료:" not in result.stdout
    assert list(destination.iterdir()) == [previous]
    assert previous.read_text() == "keep"


def test_backup_rejects_symlink_destination_and_unknown_option(operations):
    repo, run, calls = operations
    (repo.parent / "backups").symlink_to(repo, target_is_directory=True)
    assert run("backup.sh").returncode != 0
    assert not any("exec pg_dump" in line for line in calls())
    count = len(calls())
    assert run("backup.sh", "--delete").returncode == 2
    assert run("backup.sh", "--help").returncode == 0
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
        result = run("backup_run.sh")
        assert result.returncode == 0, result.stderr
    service = units / "seulseul-db-backup.service"
    timer = units / "seulseul-db-backup.timer"
    assert service.stat().st_mode & 0o777 == 0o600
    assert timer.stat().st_mode & 0o777 == 0o600
    assert 'ExecStart=/bin/bash "' + str(repo / "backup.sh") + '"' in service.read_text()
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


def test_stop_disables_only_timer_even_without_database_configuration(scheduling):
    repo, run, calls, units = scheduling
    assert run("backup_run.sh").returncode == 0
    (repo / ".env").unlink()
    (repo / "no-linger").touch()
    before = calls()
    for _ in range(2):
        assert run("backup_stop.sh").returncode == 0
    assert calls() == before
    commands = (repo / "systemctl-calls").read_text()
    assert "--user disable --now seulseul-db-backup.timer" in commands
    assert "stop seulseul-db-backup.service" not in commands
    assert (units / "seulseul-db-backup.service").exists()


def test_schedule_requires_linger_and_preserves_foreign_units(scheduling):
    repo, run, _, units = scheduling
    (repo / "no-linger").touch()
    result = run("backup_run.sh")
    assert result.returncode != 0
    assert "enable-linger" in result.stderr
    assert not units.exists()
    (repo / "no-linger").unlink()
    units.mkdir(parents=True)
    timer = units / "seulseul-db-backup.timer"
    timer.write_text("# someone else's timer\n")
    for script in ("backup_run.sh", "backup_stop.sh"):
        assert run(script).returncode != 0
        assert timer.read_text() == "# someone else's timer\n"
    timer.unlink()
    timer.symlink_to(repo / ".env")
    assert run("backup_run.sh").returncode != 0
    assert (repo / ".env").read_text() == "SECRET=never_print_this\n"


def test_schedule_errors_do_not_report_success(scheduling):
    repo, run, _, _ = scheduling
    (repo / "fail-systemctl").write_text("enable")
    result = run("backup_run.sh")
    assert result.returncode == 19
    assert "등록 완료" not in result.stdout


def test_schedule_help_invalid_arguments_and_absent_stop(scheduling):
    repo, run, calls, _ = scheduling
    for script in ("backup_run.sh", "backup_stop.sh"):
        assert run(script, "--help").returncode == 0
        assert run(script, "--delete").returncode == 2
    assert calls() == []
    assert not (repo / "systemctl-calls").exists()
    assert run("backup_stop.sh").returncode == 0


def test_schedule_escapes_systemd_specifiers_and_variables(scheduling):
    repo, _, _, units = scheduling
    renamed = repo.with_name('app % $ "quoted"')
    repo.rename(renamed)
    result = subprocess.run(
        ["bash", str(renamed / "backup_run.sh")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    service = (units / "seulseul-db-backup.service").read_text()
    assert 'app %% $$ \\"quoted\\"/backup.sh' in service
