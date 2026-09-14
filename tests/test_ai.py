"""AI 클라이언트와 공지 요약기가 실제 AI 서버 없이 올바르게 동작하는지 확인한다."""

import json
from collections.abc import Callable

import httpx
import pytest

from seulseul.ai.client import AiClientError, OpenAICompatibleChatClient
from seulseul.ai.service import (
    SUMMARY_SYSTEM_PROMPT,
    NoticeSummarizer,
    remove_reasoning_blocks,
)

SECRET_API_KEY = "nvapi-test-secret-key"


def create_client(
    handler: Callable[[httpx.Request], httpx.Response], timeout_seconds: float = 5.0
) -> OpenAICompatibleChatClient:
    return OpenAICompatibleChatClient(
        base_url="https://ai.example.test/v1",
        api_key=SECRET_API_KEY,
        model="test-model",
        timeout_seconds=timeout_seconds,
        transport=httpx.MockTransport(handler),
    )


def completion_response(content: object) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


class FakeChatClient:
    """실제 AI 대신 정해진 답변을 돌려주고 받은 프롬프트를 기록한다."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.response


def test_complete_sends_openai_compatible_request() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return completion_response("요약: 테스트")

    result = create_client(handler).complete("시스템 지시", "사용자 입력")

    assert result == "요약: 테스트"
    request = captured_requests[0]
    assert str(request.url) == "https://ai.example.test/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {SECRET_API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "시스템 지시"},
        {"role": "user", "content": "사용자 입력"},
    ]
    assert body["stream"] is False


def test_complete_reports_status_code_without_leaking_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    with pytest.raises(AiClientError) as error:
        create_client(handler).complete("시스템", "사용자")

    assert "상태 코드: 401" in str(error.value)
    assert SECRET_API_KEY not in str(error.value)


def test_complete_reports_timeout_with_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(AiClientError, match="5초 안에 오지 않았습니다"):
        create_client(handler, timeout_seconds=5.0).complete("시스템", "사용자")


def test_complete_reports_unreachable_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(AiClientError, match="연결하지 못했습니다.*ConnectError"):
        create_client(handler).complete("시스템", "사용자")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        completion_response(None),
    ],
)
def test_complete_rejects_unexpected_response_shape(response: httpx.Response) -> None:
    with pytest.raises(AiClientError, match="모델: test-model"):
        create_client(lambda request: response).complete("시스템", "사용자")


def test_summarize_sends_notice_with_summary_prompt() -> None:
    client = FakeChatClient("요약: 과제 제출\n할 일:\n- 제출\n마감: 9월 20일")

    summary = NoticeSummarizer(client).summarize("9월 20일까지 과제 제출")

    assert summary == "요약: 과제 제출\n할 일:\n- 제출\n마감: 9월 20일"
    system_prompt, user_prompt = client.calls[0]
    assert system_prompt == SUMMARY_SYSTEM_PROMPT
    assert user_prompt.endswith("9월 20일까지 과제 제출")


def test_summarize_sends_only_leading_part_of_long_notice() -> None:
    client = FakeChatClient("요약: 긴 공지")

    NoticeSummarizer(client, max_input_chars=10).summarize("가" * 20)

    _, user_prompt = client.calls[0]
    assert user_prompt.endswith("가" * 10)
    assert "가" * 11 not in user_prompt


def test_summarize_raises_when_model_returns_only_reasoning() -> None:
    client = FakeChatClient("<think>추론만 하고 답을 안 함</think>")

    with pytest.raises(AiClientError, match="빈 요약"):
        NoticeSummarizer(client).summarize("공지")


def test_summarizer_rejects_invalid_input_limit() -> None:
    with pytest.raises(ValueError, match="전달된 값: 0"):
        NoticeSummarizer(FakeChatClient("요약"), max_input_chars=0)


@pytest.mark.parametrize(
    ("raw_output", "expected"),
    [
        ("<think>생각\n중</think>\n요약: A", "요약: A"),
        ("<THINK>x</THINK>요약: B", "요약: B"),
        ("요약: C\n<think>응답 길이 제한으로 닫히지 않은 추론", "요약: C"),
        ("  요약: D  ", "요약: D"),
    ],
)
def test_remove_reasoning_blocks(raw_output: str, expected: str) -> None:
    assert remove_reasoning_blocks(raw_output) == expected
