"""Slack Web API 호출 경계."""

from collections.abc import Mapping
from typing import Any, Protocol

from slack_sdk.errors import SlackClientError


class MessagePermalinkProvider(Protocol):
    def get_message_permalink(self, channel_id: str, message_ts: str) -> str: ...


class SlackWebClientProtocol(Protocol):
    def chat_getPermalink(self, **kwargs: str) -> Mapping[str, Any]: ...


class MessagePermalinkError(Exception):
    """Slack 원본 메시지 링크를 가져오지 못했을 때 발생한다."""


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
