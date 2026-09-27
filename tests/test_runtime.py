"""운영 봇 실행 경계와 로그 시간대 정책을 검증한다."""

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from seulseul.main import SEOUL_TIMEZONE, SeoulFormatter, configure_logging

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_bot.sh"


def test_bot_entrypoint_disables_bolt_auto_oauth_without_affecting_other_env(monkeypatch):
    assert SCRIPT.is_file()
    monkeypatch.setenv("SLACK_CLIENT_ID", "client-id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-production")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "bash",
            "-c",
            'test -z "${SLACK_CLIENT_ID+x}" && '
            'test -z "${SLACK_CLIENT_SECRET+x}" && '
            'test "$SLACK_BOT_TOKEN" = xoxb-production',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_bot_uses_the_static_token_entrypoint():
    compose = (ROOT / "compose.prod.yaml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert 'entrypoint: ["/app/scripts/run_bot.sh"]' in compose
    assert 'command: ["python", "-m", "seulseul.main"]' in compose
    assert "COPY scripts/run_bot.sh ./scripts/run_bot.sh" in dockerfile


def test_production_app_containers_use_seoul_timezone_for_library_logs():
    compose = (ROOT / "compose.prod.yaml").read_text()

    assert "TZ: Asia/Seoul" in compose


def test_seoul_formatter_converts_utc_record_time_to_korean_time():
    formatter = SeoulFormatter("%(asctime)s %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="작업 완료",
        args=(),
        exc_info=None,
    )
    record.created = datetime(2026, 9, 27, 23, 40, 7, tzinfo=timezone.utc).timestamp()
    record.msecs = 0

    assert formatter.formatTime(record) == "2026-09-28 08:40:07,000"
    assert SEOUL_TIMEZONE.key == "Asia/Seoul"


def test_configure_logging_installs_seoul_formatter():
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    try:
        configure_logging()
        assert root_logger.handlers
        assert all(
            isinstance(handler.formatter, SeoulFormatter) for handler in root_logger.handlers
        )
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
