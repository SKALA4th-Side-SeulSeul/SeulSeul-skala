"""Slack Web API와 명령 응답 전송 경계."""

import logging
import math
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from seulseul.checklists.model import ChecklistDeliveryError, DailyChecklistBoard
from seulseul.slack.views import build_daily_checklist_message

logger = logging.getLogger(__name__)


class SlackCommandResponder:
    """Bolt의 respond 콜백을 통해 명령 실행자에게만 응답을 전송한다."""

    def __init__(self, respond: Callable[..., Any]) -> None:
        self._respond = respond

    def send(self, text: str) -> None:
        # 버튼 response_url의 안내가 원본 체크리스트를 교체하거나 삭제하지 않게 한다.
        self._respond(
            text, response_type="ephemeral", replace_original=False, delete_original=False
        )


class MessagePermalinkProvider(Protocol):
    def get_message_permalink(self, channel_id: str, message_ts: str) -> str: ...


class UserProfileProvider(Protocol):
    def get_real_name(self, user_id: str) -> str: ...


class SlackWebClientProtocol(Protocol):
    def chat_getPermalink(self, **kwargs: str) -> Mapping[str, Any]: ...

    def users_info(self, **kwargs: str) -> Mapping[str, Any]: ...


class MessagePermalinkError(Exception):
    """Slack 원본 메시지 링크를 가져오지 못했을 때 발생한다."""

    def __init__(self, message: str, *, auto_retryable: bool = False) -> None:
        super().__init__(message)
        self.auto_retryable = auto_retryable


class UserProfileError(Exception):
    """Slack 사용자의 성명을 가져오지 못했을 때 발생한다."""


class SlackWebApiClient:
    def __init__(self, client: SlackWebClientProtocol) -> None:
        self._client = client

    @classmethod
    def from_token(cls, token: str) -> "SlackWebApiClient":
        return cls(WebClient(token=token, timeout=10, retry_handlers=[]))

    def get_message_permalink(self, channel_id: str, message_ts: str) -> str:
        try:
            response = self._client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        except SlackApiError as error:
            raise MessagePermalinkError(
                "Slack 원문 링크 조회에 실패했습니다.",
                auto_retryable=(
                    error.response.status_code == 429
                    or (error.response.status_code or 0) >= 500
                    or error.response.get("error")
                    in {"ratelimited", "internal_error", "request_timeout"}
                ),
            ) from error
        except (SlackClientError, OSError) as error:
            raise MessagePermalinkError(
                f"Slack 원문 링크를 가져오지 못했습니다: {type(error).__name__}",
                auto_retryable=True,
            ) from error
        permalink = response.get("permalink")
        if not isinstance(permalink, str) or not permalink:
            raise MessagePermalinkError("Slack 응답에 원문 링크가 없습니다.")
        return permalink

    def get_real_name(self, user_id: str) -> str:
        try:
            response = self._client.users_info(user=user_id)
        except SlackClientError as error:
            raise UserProfileError(
                f"Slack 성명을 가져오지 못했습니다: {type(error).__name__}"
            ) from error
        user = response.get("user")
        profile = user.get("profile") if isinstance(user, Mapping) else None
        real_name = profile.get("real_name") if isinstance(profile, Mapping) else None
        if not isinstance(real_name, str) or not real_name.strip():
            raise UserProfileError("Slack 프로필에 성명이 설정되어 있지 않습니다.")
        return real_name.strip()


class SlackChecklistClient:
    """최초 일반 DM 전송·편집. 자동 HTTP 재시도로 첫 DM이 중복되지 않게 한다."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._use_container = True

    def _deliver(self, method: str, board: DailyChecklistBoard, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return self._call(
                method,
                **build_daily_checklist_message(board, use_container=self._use_container),
                **kwargs,
            )
        except ChecklistDeliveryError as error:
            if error.code != "invalid_blocks" or error.uncertain or not self._use_container:
                raise
            # Slack이 명시적으로 거절한 요청만 재구성한다. 응답 유실 때는 재전송하지 않는다.
            self._use_container = False
            logger.info("Slack 카드 서식 미지원: 기본 레이아웃으로 전환합니다.")
            return self._call(
                method, **build_daily_checklist_message(board, use_container=False), **kwargs
            )

    @classmethod
    def from_token(cls, token: str) -> "SlackChecklistClient":
        return cls(WebClient(token=token, timeout=10, retry_handlers=[]))

    def _call(self, method: str, *, posting: bool = False, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return getattr(self._client, method)(**kwargs)
        except SlackApiError as error:
            raw_code = str(error.response.get("error") or "slack_error")
            code = raw_code if re.fullmatch(r"[a-z_]+", raw_code) else "slack_error"
            try:
                retry_after = max(1, float(error.response.headers.get("Retry-After", 60)))
            except (ValueError, TypeError):
                retry_after = 60
            if not math.isfinite(retry_after):
                retry_after = 60
            raise ChecklistDeliveryError(
                code,
                uncertain=posting
                and (
                    (error.response.status_code or 0) >= 500
                    or code in {"internal_error", "fatal_error", "request_timeout"}
                ),
                retry_after=retry_after,
            ) from error
        except (SlackClientError, OSError) as error:
            raise ChecklistDeliveryError("slack_connection_error", uncertain=posting) from error

    def workspace_id(self) -> str:
        response = self._call("auth_test")
        team_id = response.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise ChecklistDeliveryError("invalid_workspace_response")
        return team_id

    def send_withdrawal(self, user_id: str, deleted: bool) -> None:
        """해지 처리 후 일반 DM으로 안내한다. 임시 응답과 자동 재전송은 사용하지 않는다."""
        self._send_plain_dm(
            user_id,
            "SeulSeul 해지가 완료되었습니다. 다시 이용하려면 `/seulseul 시작`을 입력해 주세요."
            if deleted
            else "현재 SeulSeul에 가입되어 있지 않습니다.",
        )

    def send_enrollment_guidance(self, user_id: str, guidance: str) -> None:
        """성명 불일치 안내만 일반 DM으로 보낸다. 기존 메시지는 정리하지 않는다."""
        self._send_plain_dm(user_id, guidance)

    def _send_plain_dm(self, user_id: str, text: str) -> None:
        opened = self._call("conversations_open", users=user_id)
        channel = opened.get("channel")
        channel_id = channel.get("id") if isinstance(channel, Mapping) else None
        if not isinstance(channel_id, str) or not channel_id.startswith("D"):
            raise ChecklistDeliveryError("invalid_dm_response")
        self._call(
            "chat_postMessage",
            posting=True,
            channel=channel_id,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )

    def delete_previous_messages(self, workspace_id: str, user_id: str) -> None:
        """실행자의 1:1 DM에서 인증된 봇이 쓴 일반 메시지만 삭제한다."""
        identity = self._call("auth_test")
        bot_user = identity.get("user_id")
        if identity.get("team_id") != workspace_id or not isinstance(bot_user, str) or not bot_user:
            raise ChecklistDeliveryError("invalid_workspace_response")
        opened = self._call("conversations_open", users=user_id)
        channel = opened.get("channel")
        channel_id = channel.get("id") if isinstance(channel, Mapping) else None
        if not isinstance(channel_id, str) or not channel_id.startswith("D"):
            raise ChecklistDeliveryError("invalid_dm_response")

        def pages(method: str, **kwargs: Any):
            cursor = ""
            seen = set()
            while True:
                response = self._call(
                    method, channel=channel_id, limit=100, cursor=cursor, **kwargs
                )
                messages = response.get("messages")
                if not isinstance(messages, list):
                    raise ChecklistDeliveryError("invalid_history_response")
                yield from messages
                cursor = (response.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor:
                    if response.get("has_more"):
                        raise ChecklistDeliveryError("incomplete_history_response")
                    break
                if cursor in seen:
                    raise ChecklistDeliveryError("invalid_history_cursor")
                seen.add(cursor)

        # 페이지 순회 중 삭제하면 커서가 어긋날 수 있어 먼저 대상 전체를 수집한다.
        timestamps: set[str] = set()

        def collect(message: Mapping[str, Any]) -> None:
            if message.get("user") == bot_user or (
                identity.get("bot_id") and message.get("bot_id") == identity["bot_id"]
            ):
                ts = message.get("ts")
                if not isinstance(ts, str) or not re.fullmatch(r"\d+\.\d+", ts):
                    raise ChecklistDeliveryError("invalid_history_response")
                timestamps.add(ts)

        for message in pages("conversations_history"):
            collect(message)
            if message.get("reply_count", 0):
                for reply in pages("conversations_replies", ts=message["ts"]):
                    collect(reply)
        # 답글부터 지우고 부모 메시지를 마지막에 지운다.
        for ts in sorted(
            timestamps, key=lambda value: tuple(map(int, value.split("."))), reverse=True
        ):
            try:
                self._call("chat_delete", channel=channel_id, ts=ts)
            except ChecklistDeliveryError as error:
                if error.code != "message_not_found":
                    raise

    def send(self, user_id: str, board: DailyChecklistBoard) -> tuple[str, str]:
        opened = self._call("conversations_open", users=user_id)
        channel = opened.get("channel")
        channel_id = channel.get("id") if isinstance(channel, Mapping) else None
        if not isinstance(channel_id, str) or not channel_id.startswith("D"):
            raise ChecklistDeliveryError("invalid_dm_response")
        response = self._deliver(
            "chat_postMessage",
            board,
            posting=True,
            channel=channel_id,
            unfurl_links=False,
            unfurl_media=False,
            metadata={
                "event_type": "seulseul_daily_checklist",
                "event_payload": {"delivery_id": str(board.id)},
            },
        )
        ts = response.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChecklistDeliveryError("invalid_delivery_response", uncertain=True)
        return channel_id, ts

    def update(self, channel_id: str, message_ts: str, board: DailyChecklistBoard) -> None:
        self._deliver("chat_update", board, channel=channel_id, ts=message_ts)
