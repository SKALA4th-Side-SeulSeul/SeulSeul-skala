"""Slack에 보여 줄 메시지 문구를 만든다."""

import json
from typing import Any
from zoneinfo import ZoneInfo

from seulseul.checklists.model import DailyChecklistBoard
from seulseul.users.model import Student
from seulseul.users.service import REAL_NAME_GUIDE


def build_daily_checklist_message(board: DailyChecklistBoard) -> dict[str, Any]:
    """일반 DM에 보낼 Block Kit 화면. 원문 전체와 내부 오류는 포함하지 않는다."""
    seoul = ZoneInfo("Asia/Seoul")
    heading = f"{board.message_date:%m월 %d일} 체크리스트"
    counts = f"미완료 {board.pending_count}개 · 완료 {board.completed_count}개"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": f"{counts} · 마감 전 항목 · "
                    f"갱신 {board.refreshed_at.astimezone(seoul):%H:%M}",
                }
            ],
        },
    ]

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

    blocks.append(
        {
            "type": "actions",
            "elements": [
                button(
                    "미완료 항목 보기" if board.show_completed else "완료 항목 보기",
                    "pending" if board.show_completed else "completed",
                ),
                button("새로고침", "refresh"),
            ],
        }
    )
    if not board.items:
        blocks.append(
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
        label = "완료" if item.completed else "미완료"
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": f"[{label}] {item.title[:200]}\n{item.summary[:1000]}\n"
                        f"마감: {item.deadline_at.astimezone(seoul):%m/%d %H:%M}",
                    },
                    "accessory": button(
                        "완료 취소" if item.completed else "완료하기",
                        "undo" if item.completed else "complete",
                        str(item.id),
                    ),
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"<{_link(item.original_url)}|제출 링크> · "
                            f"<{_link(item.source_permalink)}|Slack 원문 보기>",
                        }
                    ],
                },
            ]
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
                    "text": f"{board.page + 1}/{board.page_count} 페이지 · "
                    "실제 제출 후 완료를 눌러 주세요.",
                }
            ],
        }
    )
    return {"text": f"{heading} — {counts}", "blocks": blocks}


def _link(url: str) -> str:
    return url.replace("|", "%7C").replace("&", "&amp;").replace("<", "%3C").replace(">", "%3E")


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
        "체크리스트는 매일 오전 9시 개인 DM으로 도착하며, DM의 버튼으로 완료·취소합니다."
    )
