"""가짜 Docker로 운영 셸의 인자·실행 순서·안전한 실패를 검증한다."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def operations(tmp_path, monkeypatch):
    repo = tmp_path / "app with spaces"
    repo.mkdir()
    (repo / "scripts").mkdir()
    for name in ("run.sh", "stop.sh", "view.sh", "scripts/operations.sh", ".env.example"):
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
"""
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries), prepend=":")

    def run(script, *args):
        return subprocess.run(
            ["bash", str(repo / script), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
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
        "build bot migrate",
        "stop bot",
        "up -d --wait --wait-timeout 180 postgres",
        "run --rm migrate",
        "up -d --no-deps --force-recreate --scale bot=1 bot",
        "ps -a",
    ]
    assert len(steps) == len(expected) + 2
    for actual, suffix in zip(steps[2:], expected, strict=True):
        assert "compose.prod.yaml" in actual
        assert actual.endswith(suffix)
    assert "never_print_this" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["build bot migrate", "run --rm migrate", "config --quiet"])
def test_run_failure_never_starts_bot(operations, failure):
    repo, run, calls = operations
    (repo / "fail-step").write_text(failure + "\n")
    assert run("run.sh").returncode == 17
    assert not any("--force-recreate" in call for call in calls())
    if failure != "run --rm migrate":
        assert not any(call.endswith("stop bot") for call in calls())


@pytest.mark.parametrize("operation", ["all", "bot", "postgres"])
def test_stop_preserves_data_and_stops_bot_before_database(operations, operation):
    _, run, calls = operations
    assert run("stop.sh", operation).returncode == 0
    steps = calls()
    assert steps[3].endswith("stop bot")
    if operation != "bot":
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
