"""config가 Slack·AI 설정을 올바르게 읽고, 잘못된 값을 구체적인 메시지로 알리는지 확인한다."""

import pytest

from seulseul.config import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_NVIDIA_BASE_URL,
    DEFAULT_OLLAMA_BASE_URL,
    OLLAMA_PLACEHOLDER_API_KEY,
    AiSettings,
    ConfigError,
    load_ai_settings,
    load_slack_settings,
)

VALID_ENVIRON = {
    "SLACK_BOT_TOKEN": "xoxb-test-bot-token",
    "SLACK_APP_TOKEN": "xapp-test-app-token",
    "SLACK_NOTICE_CHANNELS": " C0000000001, G0000000002 ,, C0000000003 ",
}


def test_load_slack_settings_parses_tokens_and_channels() -> None:
    settings = load_slack_settings(VALID_ENVIRON)

    assert settings.bot_token == "xoxb-test-bot-token"
    assert settings.app_token == "xapp-test-app-token"
    assert settings.notice_channels == ("C0000000001", "G0000000002", "C0000000003")


def test_load_slack_settings_reports_every_missing_token() -> None:
    with pytest.raises(ConfigError) as error:
        load_slack_settings({"SLACK_BOT_TOKEN": "   "})

    assert "SLACK_BOT_TOKEN" in str(error.value)
    assert "SLACK_APP_TOKEN" in str(error.value)


@pytest.mark.parametrize(
    ("key", "wrong_value", "expected_prefix"),
    [
        ("SLACK_BOT_TOKEN", "xapp-swapped-secret", "xoxb-"),
        ("SLACK_APP_TOKEN", "xoxb-swapped-secret", "xapp-"),
    ],
)
def test_load_slack_settings_rejects_wrong_prefix_without_leaking_token(
    key: str, wrong_value: str, expected_prefix: str
) -> None:
    environ = {**VALID_ENVIRON, key: wrong_value}

    with pytest.raises(ConfigError) as error:
        load_slack_settings(environ)

    message = str(error.value)
    assert key in message
    assert expected_prefix in message
    assert wrong_value not in message


def test_load_slack_settings_rejects_empty_notice_channels() -> None:
    environ = {**VALID_ENVIRON, "SLACK_NOTICE_CHANNELS": ""}

    with pytest.raises(ConfigError, match="SLACK_NOTICE_CHANNELS"):
        load_slack_settings(environ)


def test_load_slack_settings_rejects_channel_names() -> None:
    environ = {**VALID_ENVIRON, "SLACK_NOTICE_CHANNELS": "#전체공지,C0000000001"}

    with pytest.raises(ConfigError, match="Slack 채널 ID.*#전체공지"):
        load_slack_settings(environ)


@pytest.mark.parametrize("provider", ["", "   ", "none", "NONE"])
def test_load_ai_settings_returns_none_when_ai_disabled(provider: str) -> None:
    assert load_ai_settings({"AI_PROVIDER": provider}) is None
    assert load_ai_settings({}) is None


def test_load_ai_settings_rejects_unknown_provider() -> None:
    with pytest.raises(ConfigError, match="nvidia, ollama 중 하나.*현재 값: openai"):
        load_ai_settings({"AI_PROVIDER": "openai"})


def test_load_ai_settings_reads_nvidia_with_default_base_url() -> None:
    settings = load_ai_settings(
        {
            "AI_PROVIDER": "NVIDIA",
            "NVIDIA_API_KEY": "nvapi-secret",
            "NVIDIA_MODEL": "vendor/test-model",
            "NVIDIA_BASE_URL": "",
        }
    )

    assert settings == AiSettings(
        provider="nvidia",
        base_url=DEFAULT_NVIDIA_BASE_URL,
        api_key="nvapi-secret",
        model="vendor/test-model",
        timeout_seconds=DEFAULT_AI_TIMEOUT_SECONDS,
    )


def test_load_ai_settings_reports_missing_nvidia_values_without_leaking_key() -> None:
    with pytest.raises(ConfigError) as error:
        load_ai_settings({"AI_PROVIDER": "nvidia", "NVIDIA_API_KEY": "nvapi-secret"})

    message = str(error.value)
    assert "NVIDIA_MODEL" in message
    assert "NVIDIA_API_KEY" not in message
    assert "nvapi-secret" not in message


def test_load_ai_settings_reads_ollama_with_placeholder_key_and_custom_url() -> None:
    settings = load_ai_settings(
        {
            "AI_PROVIDER": "ollama",
            "OLLAMA_MODEL": "qwen3.5:2b",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1/",
            "AI_TIMEOUT_SECONDS": "120",
        }
    )

    assert settings == AiSettings(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        api_key=OLLAMA_PLACEHOLDER_API_KEY,
        model="qwen3.5:2b",
        timeout_seconds=120.0,
    )


def test_load_ai_settings_uses_default_ollama_url() -> None:
    settings = load_ai_settings({"AI_PROVIDER": "ollama", "OLLAMA_MODEL": "llama3.2"})

    assert settings is not None
    assert settings.base_url == DEFAULT_OLLAMA_BASE_URL


def test_load_ai_settings_requires_ollama_model() -> None:
    with pytest.raises(ConfigError, match="OLLAMA_MODEL"):
        load_ai_settings({"AI_PROVIDER": "ollama"})


@pytest.mark.parametrize("raw_timeout", ["0", "-5", "abc", "nan", "inf"])
def test_load_ai_settings_rejects_invalid_timeout(raw_timeout: str) -> None:
    environ = {
        "AI_PROVIDER": "ollama",
        "OLLAMA_MODEL": "llama3.2",
        "AI_TIMEOUT_SECONDS": raw_timeout,
    }

    with pytest.raises(ConfigError, match=f"현재 값: {raw_timeout}"):
        load_ai_settings(environ)
