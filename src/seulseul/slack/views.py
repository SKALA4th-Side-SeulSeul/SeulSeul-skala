"""Slack에 보여 줄 메시지 문구를 만든다."""

import json
from html import escape
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from seulseul.checklists.model import DailyChecklistBoard
from seulseul.users.model import Student
from seulseul.users.service import REAL_NAME_GUIDE


def build_daily_checklist_message(
    board: DailyChecklistBoard, *, use_container: bool = True
) -> dict[str, Any]:
    """일반 DM에 보낼 Block Kit 화면. 원문 전체와 내부 오류는 포함하지 않는다."""
    seoul = ZoneInfo("Asia/Seoul")
    heading = "내 체크리스트"
    counts = f"미완료 {board.pending_count}개 · 완료 {board.completed_count}개"
    view_hint = (
        f"완료 목록 · {board.completed_count}개"
        if board.show_completed
        else f"미완료 목록 · {board.pending_count}개"
    )
    rows: list[dict[str, Any]] = []

    def button(label: str, operation: str, item_id: str | None = None) -> dict[str, Any]:
        value = {"daily": str(board.id)}
        if item_id is not None:
            value["item"] = item_id
        return {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": f"checklist_{operation}",
            "value": json.dumps(value),
        }

    if not board.items:
        rows.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": "완료한 항목이 없어요."
                    if board.show_completed
                    else "현재 남은 할 일이 없어요.",
                },
            }
        )
    for item in board.items:
        title = _short_title(item.title)
        link_label = escape(title, quote=False).replace("|", "｜")
        control = button(
            "↶" if item.completed else "✓", "undo" if item.completed else "complete", str(item.id)
        )
        control["accessibility_label"] = (
            f"{title}: {'완료 취소' if item.completed else '완료로 표시'}"
        )
        rows.extend(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{_link_icon(item.original_url)} "
                        f"*<{_link(item.original_url)}|{link_label}>*",
                    },
                    "accessory": control,
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "plain_text", "text": _link_domain(item.original_url)},
                        {
                            "type": "mrkdwn",
                            "text": f"· {item.deadline_at.astimezone(seoul):%m/%d %H:%M} 마감 · "
                            f"<{_link(item.source_permalink)}|원문>",
                        },
                    ],
                },
            ]
        )
    if use_container:
        blocks = [
            {
                "type": "container",
                "title": {"type": "plain_text", "text": heading},
                "subtitle": {"type": "plain_text", "text": view_hint},
                "width": "full",
                "has_header_divider": True,
                "child_blocks": rows,
            }
        ]
    else:
        # 최신 카드 서식을 받지 못하는 워크스페이스도 동일한 정보·버튼을 제공한다.
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": heading}},
            {"type": "context", "elements": [{"type": "plain_text", "text": view_hint}]},
            *rows,
        ]
    blocks.append(
        {
            "type": "actions",
            "elements": [
                button(
                    f"미완료 보기 ({board.pending_count})"
                    if board.show_completed
                    else f"완료 보기 ({board.completed_count})",
                    "pending" if board.show_completed else "completed",
                ),
                button("새로고침", "refresh"),
            ],
        }
    )
    navigation = []
    if board.page > 0:
        navigation.append(button("이전", "previous"))
    if board.page + 1 < board.page_count:
        navigation.append(button("다음", "next"))
    if navigation:
        blocks.append({"type": "actions", "elements": navigation})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": (
                        f"{board.page + 1}/{board.page_count} · " if board.page_count > 1 else ""
                    )
                    + "제출 후 ✓ · "
                    f"마지막 갱신 {board.refreshed_at.astimezone(seoul):%m/%d %H:%M}",
                }
            ],
        }
    )
    return {"text": f"{heading} — {counts}\n{view_hint}", "blocks": blocks}


def _link(url: str) -> str:
    return url.replace("|", "%7C").replace("&", "&amp;").replace("<", "%3C").replace(">", "%3E")


def _short_title(title: str) -> str:
    compact = " ".join(title.split())
    return compact if len(compact) <= 28 else compact[:27].rstrip() + "…"


def _link_domain(url: str) -> str:
    try:
        return urlsplit(url).hostname or "링크"
    except ValueError:
        return "링크"


def _link_icon(url: str) -> str:
    """검증된 공지 링크의 표시용 아이콘만 선택한다. 수집·배정 조건은 바꾸지 않는다."""
    try:
        parsed = urlsplit(url)
        label = f"{parsed.hostname or ''}{unquote(parsed.path)}".lower()
    except ValueError:
        return "🔗"
    if "form" in label:
        return "📝"
    if "docs" in label:
        return "📄"
    return "🔗"


def build_enrollment_success_text(user_id: str, student: Student) -> str:
    return f"<@{user_id}> SeulSeul 가입이 완료되었습니다. 소속: 광주 {student.class_number}반"


def build_invalid_real_name_text(user_id: str) -> str:
    return (
        f"<@{user_id}> 가입하려면 Slack 성명을 "
        f"`{REAL_NAME_GUIDE}` 형식으로 설정해 주세요. 예: `4기_광주_3반_홍길동`"
    )


def build_withdrawal_text(user_id: str, deleted: bool) -> str:
    if deleted:
        return f"<@{user_id}> 알림이 해지되었고 개인 체크리스트가 삭제되었습니다."
    return f"<@{user_id}> 현재 가입된 정보가 없습니다."


def build_command_help_text(user_id: str) -> str:
    return (
        f"<@{user_id}> `/seulseul 시작`으로 가입하고, "
        "`/seulseul 해지`로 알림을 해지할 수 있습니다. "
        "시작·해지 시 기존 봇 DM을 정리합니다. 시작하면 완료 기록은 유지한 채 "
        "새 체크리스트 하나를 보내고 같은 메시지를 계속 갱신합니다. "
        "제출 후 ✓로 완료하고 ↶로 취소할 수 있습니다."
    )
