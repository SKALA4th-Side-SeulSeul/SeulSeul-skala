"""Slack Web API와 명령 응답 전송 경계."""

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from slack_sdk.errors import SlackClientError


class SlackCommandResponder:
    """Bolt의 respond 콜백을 통해 명령 실행자에게만 응답을 전송한다."""

    def __init__(self, respond: Callable[..., Any]) -> None:
        self._respond = respond

    def send(self, text: str) -> None:
        self._respond(text, response_type="ephemeral")


class MessagePermalinkProvider(Protocol):
    def get_message_permalink(self, channel_id: str, message_ts: str) -> str: ...


class UserProfileProvider(Protocol):
    def get_display_name(self, user_id: str) -> str: ...


class SlackWebClientProtocol(Protocol):
    def chat_getPermalink(self, **kwargs: str) -> Mapping[str, Any]: ...

    def users_info(self, **kwargs: str) -> Mapping[str, Any]: ...


class MessagePermalinkError(Exception):
    """Slack 원본 메시지 링크를 가져오지 못했을 때 발생한다."""


class UserProfileError(Exception):
    """Slack 사용자의 표시 이름을 가져오지 못했을 때 발생한다."""


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

    def get_display_name(self, user_id: str) -> str:
        try:
            response = self._client.users_info(user=user_id)
        except SlackClientError as error:
            raise UserProfileError(
                f"Slack 표시 이름을 가져오지 못했습니다: {type(error).__name__}"
            ) from error
        user = response.get("user")
        profile = user.get("profile") if isinstance(user, Mapping) else None
        display_name = profile.get("display_name") if isinstance(profile, Mapping) else None
        if not isinstance(display_name, str) or not display_name.strip():
            raise UserProfileError("Slack 프로필에 표시 이름이 설정되어 있지 않습니다.")
        return display_name.strip()
