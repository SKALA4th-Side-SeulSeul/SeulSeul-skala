"""Slack 앱 manifest의 표시 이름 설정을 검증한다."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_slack_manifest_uses_korean_app_name_and_ascii_bot_identifier():
    manifest = (ROOT / "slack-manifest.yaml").read_text()

    assert "  name: 슬슬" in manifest
    assert "    display_name: seulseul" in manifest
