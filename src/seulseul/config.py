"""환경변수를 읽고 검증하는 유일한 모듈.

다른 모듈은 os.environ을 직접 읽지 않고 이 모듈의 설정 객체를 사용한다(Ruff TID251).
로컬 개발에서는 프로젝트 루트의 .env를 읽으며, 이미 설정된 환경변수는 덮어쓰지 않는다.
"""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# src/seulseul/config.py에서 두 단계 위가 프로젝트 루트다.
# 편집 설치(pip install -e) 환경을 전제로 한다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TOKEN_PREFIXES = {
    "SLACK_BOT_TOKEN": "xoxb-",
    "SLACK_APP_TOKEN": "xapp-",
}

AI_PROVIDERS = ("nvidia", "ollama")
DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
# 로컬 모델은 첫 요청에 모델을 메모리에 올리는 시간이 들어 넉넉하게 잡는다.
DEFAULT_AI_TIMEOUT_SECONDS = 45.0
# Ollama는 API 키를 검사하지 않지만 OpenAI 호환 요청 형식상 값이 필요하다.
OLLAMA_PLACEHOLDER_API_KEY = "ollama"
SLACK_CHANNEL_ID_PATTERN = re.compile(r"^[CG][A-Z0-9]+$")


class ConfigError(Exception):
    """필수 환경변수가 없거나 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class SlackSettings:
    bot_token: str
    app_token: str
    # 채널 이름은 바뀔 수 있으므로 Slack 채널 ID만 사용한다.
    notice_channels: tuple[str, ...]
    # 봇 이벤트는 받지 않고 운영자 CLI로만 처리하는 채널입니다.
    manual_notice_channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class AiSettings:
    provider: str
    base_url: str
    # 비밀값이므로 로그나 오류 메시지에 출력하지 않는다.
    api_key: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class DatabaseSettings:
    url: str


def load_notice_targets(
    notice_channels: tuple[str, ...],
    environ: Mapping[str, str] | None = None,
    *,
    manual_notice_channels: tuple[str, ...] = (),
) -> dict[str, int | None]:
    """공지 채널별 광주 전체(None) 또는 반 번호를 명시적으로 읽는다."""
    environ = _resolve_environ(environ)
    configured_channels = (*notice_channels, *manual_notice_channels)
    raw = _read(environ, "SLACK_NOTICE_TARGETS")
    targets: dict[str, int | None] = {}
    for entry in raw.split(","):
        channel, separator, target = entry.strip().partition("=")
        channel, target = channel.strip(), target.strip()
        if (
            not separator
            or channel in targets
            or channel not in configured_channels
            or target not in {"all", "1", "2", "3", "4"}
        ):
            raise ConfigError(
                "SLACK_NOTICE_TARGETS에 각 공지 채널을 채널ID=all 또는 채널ID=1~4로 "
                "쉼표로 구분해 입력하세요. 중복·미등록 채널은 허용하지 않습니다."
            )
        targets[channel] = None if target == "all" else int(target)
    if set(targets) != set(configured_channels):
        raise ConfigError("SLACK_NOTICE_TARGETS에 자동·수동 공지 채널의 모든 채널이 필요합니다.")
    return targets


def load_slack_settings(environ: Mapping[str, str] | None = None) -> SlackSettings:
    """Slack 연결 설정을 읽는다.

    environ을 넘기면 .env와 실제 환경변수를 읽지 않는다(테스트용).
    """
    environ = _resolve_environ(environ)
    _raise_if_missing(environ, [*REQUIRED_TOKEN_PREFIXES, "SLACK_NOTICE_CHANNELS"])

    # 토큰 값은 오류 메시지에 넣지 않는다. 로그에 비밀값이 남는 것을 막기 위함이다.
    for key, prefix in REQUIRED_TOKEN_PREFIXES.items():
        if not _read(environ, key).startswith(prefix):
            raise ConfigError(
                f"{key}는 '{prefix}'로 시작해야 합니다. "
                "Bot 토큰과 앱 수준 토큰이 바뀌지 않았는지 확인하세요."
            )

    notice_channels = tuple(
        channel.strip()
        for channel in _read(environ, "SLACK_NOTICE_CHANNELS").split(",")
        if channel.strip()
    )
    invalid_channels = [
        channel
        for channel in notice_channels
        if SLACK_CHANNEL_ID_PATTERN.fullmatch(channel) is None
    ]
    if invalid_channels:
        raise ConfigError(
            "SLACK_NOTICE_CHANNELS에는 채널 이름이 아니라 C 또는 G로 시작하는 "
            f"Slack 채널 ID를 입력하세요. 잘못된 값: {', '.join(invalid_channels)}"
        )

    manual_notice_channels = tuple(
        channel.strip()
        for channel in _read(environ, "SLACK_MANUAL_NOTICE_CHANNELS").split(",")
        if channel.strip()
    )
    invalid_manual_channels = [
        channel
        for channel in manual_notice_channels
        if SLACK_CHANNEL_ID_PATTERN.fullmatch(channel) is None
    ]
    if invalid_manual_channels:
        raise ConfigError(
            "SLACK_MANUAL_NOTICE_CHANNELS에는 C 또는 G로 시작하는 Slack 채널 ID만 "
            f"허용합니다. 잘못된 값: {', '.join(invalid_manual_channels)}"
        )
    if set(notice_channels) & set(manual_notice_channels):
        raise ConfigError("SLACK_MANUAL_NOTICE_CHANNELS는 자동 공지 채널과 중복될 수 없습니다.")

    return SlackSettings(
        bot_token=_read(environ, "SLACK_BOT_TOKEN"),
        app_token=_read(environ, "SLACK_APP_TOKEN"),
        notice_channels=notice_channels,
        manual_notice_channels=manual_notice_channels,
    )


def load_ai_settings(environ: Mapping[str, str] | None = None) -> AiSettings | None:
    """AI 요약 설정을 읽는다. AI_PROVIDER가 비어 있으면 None을 반환한다.

    environ을 넘기면 .env와 실제 환경변수를 읽지 않는다(테스트용).
    """
    environ = _resolve_environ(environ)
    provider = _read(environ, "AI_PROVIDER").lower()
    if provider in ("", "none"):
        return None
    if provider not in AI_PROVIDERS:
        raise ConfigError(
            f"AI_PROVIDER는 {', '.join(AI_PROVIDERS)} 중 하나이거나 비워 둬야 합니다. "
            f"현재 값: {provider}"
        )

    timeout_seconds = _read_timeout_seconds(environ)

    if provider == "nvidia":
        _raise_if_missing(environ, ["NVIDIA_API_KEY", "NVIDIA_MODEL"])
        return AiSettings(
            provider=provider,
            base_url=_read_base_url(environ, "NVIDIA_BASE_URL", DEFAULT_NVIDIA_BASE_URL),
            api_key=_read(environ, "NVIDIA_API_KEY"),
            model=_read(environ, "NVIDIA_MODEL"),
            timeout_seconds=timeout_seconds,
        )

    _raise_if_missing(environ, ["OLLAMA_MODEL"])
    return AiSettings(
        provider=provider,
        base_url=_read_base_url(environ, "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        api_key=OLLAMA_PLACEHOLDER_API_KEY,
        model=_read(environ, "OLLAMA_MODEL"),
        timeout_seconds=timeout_seconds,
    )


def load_database_settings(environ: Mapping[str, str] | None = None) -> DatabaseSettings:
    """PostgreSQL 연결 URL을 읽고 Psycopg 3 형식인지 검증한다."""
    environ = _resolve_environ(environ)
    _raise_if_missing(environ, ["DATABASE_URL"])
    url = _read(environ, "DATABASE_URL")
    if not url.startswith("postgresql+psycopg://"):
        raise ConfigError("DATABASE_URL은 postgresql+psycopg:// 형식이어야 합니다.")
    return DatabaseSettings(url=url)


def _resolve_environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    if environ is not None:
        return environ
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ


def _read(environ: Mapping[str, str], key: str) -> str:
    return (environ.get(key) or "").strip()


def _raise_if_missing(environ: Mapping[str, str], keys: list[str]) -> None:
    missing_keys = [key for key in keys if not _read(environ, key)]
    if missing_keys:
        raise ConfigError(
            f"필수 환경변수가 비어 있습니다: {', '.join(missing_keys)}. "
            ".env.example을 참고해 .env에 값을 채우세요."
        )


def _read_base_url(environ: Mapping[str, str], key: str, default: str) -> str:
    return (_read(environ, key) or default).rstrip("/")


def _read_timeout_seconds(environ: Mapping[str, str]) -> float:
    raw_value = _read(environ, "AI_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_AI_TIMEOUT_SECONDS
    try:
        timeout_seconds = float(raw_value)
    except ValueError:
        timeout_seconds = math.nan
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ConfigError(f"AI_TIMEOUT_SECONDS는 0보다 큰 숫자여야 합니다. 현재 값: {raw_value}")
    return timeout_seconds
