"""운영 봇 실행 경계의 OAuth 환경변수 격리를 검증한다."""

import subprocess
from pathlib import Path

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
    assert "COPY scripts/run_bot.sh ./scripts/run_bot.sh" in dockerfile
