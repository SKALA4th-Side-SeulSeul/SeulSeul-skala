"""로그 검색의 사람용 요약과 원본 진단 접근을 검증한다."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "log_search.py"


def search(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--source", "bot", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_checklist_diagnostics_are_one_readable_action_and_raw_remains_available(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text(
        "2026-10-26T08:26:16Z 2026-10-26 17:26:16,765 INFO "
        'main.py:handle_checklist_action: {"event":"checklist_action_received",'
        '"trace_id":"trace-1","actor_name":"김규진","operation":"refresh",'
        '"workspace_ref":"hash-value"}\n'
        "2026-10-26T08:26:17Z 2026-10-26 17:26:17,000 INFO "
        'service.py:handle: {"event":"checklist_action_parsed","trace_id":"trace-1"}\n'
        "2026-10-26T08:26:18Z 2026-10-26 17:26:18,000 INFO "
        'service.py:handle: {"event":"checklist_action_result","trace_id":"trace-1",'
        '"saved":true,"dm_synchronized":true}\n'
    )

    result = search(path, "--user", "김규진", "--action", "새로고침")
    assert result.returncode == 0, result.stderr
    assert "2026-10-26 17:26:18 | 정상 | bot" in result.stdout
    assert "김규진 | 체크리스트 새로고침 | 완료 · 개인 DM 갱신" in result.stdout
    assert "검색 결과 1건" in result.stdout
    assert "trace-1" not in result.stdout
    assert "workspace_ref" not in result.stdout
    assert "{" not in result.stdout

    raw = search(path, "--raw")
    assert raw.returncode == 0
    assert "검색 결과 3건" in raw.stdout
    assert '"trace_id":"trace-1"' in raw.stdout


def test_rejected_action_keeps_actor_and_reason_without_diagnostic_noise(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text(
        "2026-10-26T08:00:00Z 2026-10-26 17:00:00,000 INFO logger: "
        '{"event":"checklist_action_received","trace_id":"trace-2",'
        '"actor_name":"박하늘","operation":"complete"}\n'
        "2026-10-26T08:00:01Z 2026-10-26 17:00:01,000 WARNING logger: "
        '{"event":"checklist_action_item_unavailable","trace_id":"trace-2"}\n'
        "2026-10-26T08:00:02Z 2026-10-26 17:00:02,000 WARNING logger: "
        '{"event":"checklist_action_rejected","trace_id":"trace-2"}\n'
    )

    result = search(path, "--action", "거부")
    assert result.returncode == 0
    assert "주의 | bot" in result.stdout
    assert "박하늘 | 체크리스트 공지 완료 | 요청 거부 · 공지 항목 사용 불가" in result.stdout
    assert "검색 결과 1건" in result.stdout


def test_validation_reason_is_named_in_rejected_action(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text(
        "2026-10-26T08:00:00Z 2026-10-26 17:00:00,000 INFO logger: "
        '{"event":"checklist_action_received","trace_id":"trace-3",'
        '"actor_name":"김규진","operation":"refresh"}\n'
        "2026-10-26T08:00:01Z 2026-10-26 17:00:01,000 WARNING logger: "
        '{"event":"checklist_message_validation","trace_id":"trace-3",'
        '"result":"rejected","reason":"timestamp_mismatch"}\n'
        "2026-10-26T08:00:02Z 2026-10-26 17:00:02,000 WARNING logger: "
        '{"event":"checklist_action_rejected","trace_id":"trace-3"}\n'
    )

    result = search(path)
    assert result.returncode == 0
    assert "김규진 | 체크리스트 새로고침 | 요청 거부 · 메시지 시각 불일치" in result.stdout
    assert "검색 결과 1건" in result.stdout
