"""Slack Web API와 명령 응답 전송 경계."""

import math
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from seulseul.checklists.model import ChecklistDeliveryError, DailyChecklistBoard
from seulseul.slack.views import build_daily_checklist_message


class SlackCommandResponder:
    """Bolt의 respond 콜백을 통해 명령 실행자에게만 응답을 전송한다."""

    def __init__(self, respond: Callable[..., Any]) -> None:
        self._respond = respond

    def send(self, text: str) -> None:
        self._respond(text, response_type="ephemeral")


class MessagePermalinkProvider(Protocol):
    def get_message_permalink(self, channel_id: str, message_ts: str) -> str: ...


class UserProfileProvider(Protocol):
    def get_real_name(self, user_id: str) -> str: ...


class SlackWebClientProtocol(Protocol):
    def chat_getPermalink(self, **kwargs: str) -> Mapping[str, Any]: ...

    def users_info(self, **kwargs: str) -> Mapping[str, Any]: ...


class MessagePermalinkError(Exception):
    """Slack 원본 메시지 링크를 가져오지 못했을 때 발생한다."""


class UserProfileError(Exception):
    """Slack 사용자의 성명을 가져오지 못했을 때 발생한다."""


class SlackWebApiClient:
    def __init__(self, client: SlackWebClientProtocol) -> None:
        self._client = client

    def get_message_permalink(self, channel_id: str, message_ts: str) -> str:
        try:
            response = self._client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        except SlackClientError as error:
            raise MessagePermalinkError(
                f"Slack 원문 링크를 가져오지 못했습니다: {type(error).__name__}"
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
    """일일 일반 DM 전송·편집. 자동 HTTP 재시도로 첫 DM이 중복되지 않게 한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

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

    def send(self, user_id: str, board: DailyChecklistBoard) -> tuple[str, str]:
        opened = self._call("conversations_open", users=user_id)
        channel = opened.get("channel")
        channel_id = channel.get("id") if isinstance(channel, Mapping) else None
        if not isinstance(channel_id, str) or not channel_id.startswith("D"):
            raise ChecklistDeliveryError("invalid_dm_response")
        response = self._call(
            "chat_postMessage",
            posting=True,
            channel=channel_id,
            **build_daily_checklist_message(board),
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
        self._call(
            "chat_update", channel=channel_id, ts=message_ts, **build_daily_checklist_message(board)
        )
