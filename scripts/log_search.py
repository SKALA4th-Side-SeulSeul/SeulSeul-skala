"""현재·보관 로그를 검색하고 작업별로 읽기 쉬운 운영 기록을 만든다."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
DOCKER_TIME = re.compile(r"^(?:[^|]+\|\s*)?(\d{4}-\d\d-\d\dT\S+)\s+(.*)$")
APP_TIME = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
APP_HEADER = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "
    r"(INFO|WARNING|ERROR|DEBUG|CRITICAL) [\w.]+(?::[\w.]+)?: "
)
ACTOR = re.compile(r"\bactor=(.*?)(?=\s+[a-z_]+=|$)")
FIELDS = re.compile(r"\b([a-z_]+)=([^ ]+)")
PRIVATE = re.compile(
    r"\b(?:channel|channel_id|ts|trace|trace_id|workspace|workspace_id|user_id|"
    r"daily_id|item_id|delivery|pid|instance_id|url)=[^ ,}]+|https?://\S+"
)
OPERATIONS = {
    "refresh": "새로고침",
    "complete": "공지 완료",
    "undo": "완료 취소",
    "pending": "미완료 목록",
    "completed": "완료 목록",
    "previous": "이전 페이지",
    "next": "다음 페이지",
}
ISSUES = {
    "checklist_action_invalid_workspace": "워크스페이스 불일치",
    "checklist_action_invalid_value": "버튼 정보 오류",
    "checklist_action_student_not_found": "가입자 없음",
    "checklist_action_student_changed": "학생 정보 변경",
    "checklist_action_item_unavailable": "공지 항목 사용 불가",
}
VALIDATION_REASONS = {
    "delivery_not_found": "발송 기록 없음",
    "delivery_owner_mismatch": "소유자 불일치",
    "delivery_not_current": "현재 사용 중인 DM 아님",
    "channel_mismatch": "DM 채널 불일치",
    "timestamp_mismatch": "메시지 시각 불일치",
}
TECHNICAL = {"checklist_action_parsed", "checklist_message_validation"}


@dataclass
class Row:
    when: datetime
    service: str
    status: str
    actor: str
    action: str
    result: str
    original: str
    trace: str = ""
    event: str = ""


def local_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=SEOUL) if parsed.tzinfo is None else parsed.astimezone(SEOUL)
    except ValueError:
        match = APP_TIME.search(value)
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SEOUL)
        raise


def boundary(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SEOUL)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=SEOUL)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                "시각은 YYYY-MM-DD 또는 YYYY-MM-DD HH:MM 형식입니다."
            ) from error


def end_boundary(value: str) -> datetime:
    result = boundary(value)
    return result + timedelta(days=1) - timedelta(microseconds=1) if len(value) == 10 else result


def fields_of(message: str) -> dict[str, str]:
    return dict(FIELDS.findall(message))


def status_of(message: str) -> str:
    if re.search(r"\b(?:ERROR|CRITICAL|FATAL)\b", message):
        return "오류"
    if re.search(r"\b(?:WARNING|WARN)\b", message):
        return "주의"
    return "정상"


def clean(value: str) -> str:
    return " ".join(PRIVATE.sub("[숨김]", value).split())[:180]


def decode(line: str, service: str) -> Row | None:
    match = DOCKER_TIME.match(line)
    if match:
        try:
            when = local_time(match.group(1))
        except ValueError:
            return None
        body = match.group(2)
    else:
        match = APP_TIME.search(line)
        if not match:
            return None
        try:
            when = local_time(match.group(1))
        except ValueError:
            return None
        body = line
    status = status_of(body)
    body = APP_HEADER.sub("", body)
    if "{" in body:
        try:
            data = json.loads(body[body.index("{") :])
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and data.get("event"):
            event = str(data["event"])
            operation = OPERATIONS.get(str(data.get("operation", "")), "버튼 작업")
            action = f"체크리스트 {operation}" if event.startswith("checklist_action_") else event
            result = "기록"
            if event == "checklist_action_received":
                result = "요청 접수"
            elif event == "checklist_action_result":
                if data.get("saved") and data.get("dm_synchronized"):
                    result = "완료 · 개인 DM 갱신"
                elif data.get("saved"):
                    status, result = "주의", "저장 완료 · 개인 DM 갱신 대기"
                else:
                    status, result = "오류", "저장 실패"
            elif event == "checklist_action_rejected":
                status, result = "주의", "요청 거부"
            elif event == "checklist_action_error":
                status, result = "오류", f"처리 실패 · {data.get('error_type', '원인 확인 필요')}"
            elif event in ISSUES:
                status, result = "주의", ISSUES[event]
            elif event == "checklist_action_invalid_body":
                status, result = "주의", "버튼 요청 형식 오류"
            elif event == "checklist_message_validation":
                result = VALIDATION_REASONS.get(str(data.get("reason")), "메시지 검증")
            elif event == "checklist_delivery_saved":
                action, result = "체크리스트 DM 발송", "발송 기록 저장"
            elif event == "checklist_delivery_save_conflict":
                action, status, result = "체크리스트 DM 발송", "주의", "발송 기록 충돌"
            else:
                result = clean(str(data.get("result") or data.get("reason") or "기록"))
            return Row(
                when,
                service,
                status,
                str(data.get("actor_name") or data.get("actor") or "-"),
                action,
                result,
                line,
                str(data.get("trace_id") or ""),
                event,
            )
    fields = fields_of(body)
    actor_match = ACTOR.search(body)
    actor = (
        actor_match.group(1).strip() if actor_match else "운영자" if service == "manual" else "-"
    )
    if "명령어 처리:" in body:
        return Row(
            when,
            service,
            status,
            actor,
            f"/seulseul {fields.get('action', '명령')}",
            clean(fields.get("result", "처리 완료")),
            line,
        )
    if "수동 공지 처리" in body:
        action = f"수동 공지 {fields.get('action', '처리')}"
        if "실패:" in body:
            status, result = "오류", f"실패 · {fields.get('reason', '원인 확인 필요')}"
        elif "중단:" in body:
            status, result = "주의", "중단"
        else:
            result = "완료"
        return Row(when, service, status, actor, action, clean(result), line)
    if "공지 AI 분석 실패" in body:
        return Row(when, service, "주의", actor, "공지 AI 분석", "실패 · 재처리 필요", line)
    if "메시지 이벤트 처리:" in body:
        result = (
            f"공지 {fields.get('notices', '?')}건 · "
            f"성공 {fields.get('processed', '?')}건 · "
            f"실패 {fields.get('failed', '?')}건"
        )
        return Row(when, service, status, actor, "공지 메시지 처리", result, line)
    return Row(when, service, status, actor, service + " 기록", clean(body), line)


def summarize(rows: list[Row]) -> list[Row]:
    """같은 버튼 시도의 진단 단계들을 최종 결과 한 줄로 합친다."""
    groups: dict[str, list[Row]] = {}
    result: list[Row] = []
    for row in rows:
        if row.trace and (
            row.event.startswith("checklist_action_") or row.event == "checklist_message_validation"
        ):
            groups.setdefault(row.trace, []).append(row)
        elif row.event not in TECHNICAL:
            result.append(row)
    terminal = {
        "checklist_action_result",
        "checklist_action_rejected",
        "checklist_action_error",
        "checklist_action_invalid_body",
    }
    for group in groups.values():
        group.sort(key=lambda row: row.when)
        received = next((row for row in group if row.event == "checklist_action_received"), None)
        final = next((row for row in reversed(group) if row.event in terminal), None)
        issue = next(
            (
                row
                for row in reversed(group)
                if row.event in ISSUES
                or (row.event == "checklist_message_validation" and row.status == "주의")
            ),
            None,
        )
        row = final or received or issue
        if row is None:
            continue
        if received:
            row.actor = received.actor
            row.action = received.action
        if final and final.event == "checklist_action_rejected" and issue:
            row.result += f" · {issue.result}"
        row.original = "\n".join(item.original for item in group)
        if final is None and received:
            row.status, row.result = "주의", "요청 접수 · 결과 기록 없음"
        result.append(row)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", nargs=2, action="append", metavar=("SERVICE", "FILE"), default=[]
    )
    parser.add_argument("--from", dest="start", type=boundary)
    parser.add_argument("--to", dest="end", type=end_boundary)
    parser.add_argument("--user", default="")
    parser.add_argument("--action", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1 or (args.start and args.end and args.start > args.end):
        parser.error("검색 범위와 결과 수를 확인하세요.")
    rows: list[Row] = []
    for service, filename in args.source:
        path = Path(filename)
        if not path.is_file() or path.is_symlink():
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                row = decode(raw.rstrip("\n"), service)
                if row:
                    rows.append(row)
    if not args.raw:
        rows = summarize(rows)
    rows = [
        row
        for row in rows
        if (not args.start or row.when >= args.start)
        and (not args.end or row.when <= args.end)
        and args.user.casefold() in row.actor.casefold()
        and args.action.casefold() in (row.action + row.result + row.original).casefold()
        and args.text.casefold() in (row.original + row.action + row.result).casefold()
    ]
    rows.sort(key=lambda row: row.when, reverse=True)
    visible = rows if args.all else rows[: args.limit]
    if not args.raw:
        print("시각                | 상태 | 서비스   | 사용자 | 작업 | 결과")
    for row in visible:
        if args.raw:
            print(f"[{row.service}] {row.original}")
        else:
            print(
                f"{row.when:%Y-%m-%d %H:%M:%S} | {row.status} | {row.service:<8} | "
                f"{row.actor} | {row.action} | {row.result}"
            )
    print(
        f"검색 결과 {len(rows)}건"
        + (f" · 표시 {len(visible)}건 (--all로 전체 보기)" if len(visible) < len(rows) else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
