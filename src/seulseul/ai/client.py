"""NVIDIA와 Ollama의 chat API 클라이언트.

NVIDIA는 OpenAI 호환 `/v1/chat/completions`를 사용하고, Ollama는 Qwen3의
thinking 제어가 확실한 네이티브 `/api/chat`을 사용한다.
외부 AI 호출은 이 모듈에만 둔다(D-002). API 키는 로그와 오류 메시지에 넣지 않는다.
"""

import math
from typing import Any, Protocol

import httpx

# 같은 공지에 대한 분석이 크게 흔들리지 않도록 낮은 온도를 쓴다.
ANALYSIS_TEMPERATURE = 0.2
ANALYSIS_TOP_P = 0.8
MAX_RESPONSE_TOKENS = 512
OLLAMA_CONTEXT_TOKENS = 2048
OLLAMA_MAX_RESPONSE_TOKENS = 256
OLLAMA_KEEP_ALIVE = "5m"
OLLAMA_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "deadline_at": {"type": "string"},
        "deadline_source_text": {"type": "string"},
    },
    "required": ["title", "summary", "deadline_at", "deadline_source_text"],
}
MAX_RETRY_AFTER_SECONDS = 60.0
# 오류 응답 본문은 원인 파악에 필요한 앞부분만 메시지에 담는다.
ERROR_BODY_PREVIEW_CHARS = 200


class AiClientError(Exception):
    """AI 호출이 실패했거나 응답 형식이 예상과 다를 때 발생한다."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
        retry_count: int = 0,
        auto_retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.retry_count = retry_count
        self.auto_retryable = auto_retryable


class ChatClient(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class OpenAICompatibleChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        provider: str = "nvidia",
        disable_thinking: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if provider not in {"nvidia", "ollama"}:
            raise ValueError(f"지원하지 않는 AI 제공자입니다: {provider}")
        self._model = model
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._disable_thinking = disable_thinking
        client_base_url = _ollama_native_base_url(base_url) if provider == "ollama" else base_url
        # transport는 테스트에서 가짜 응답을 넣기 위해서만 사용한다.
        self._http_client = httpx.Client(
            base_url=client_base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """시스템 지시와 사용자 입력을 보내고 모델의 답변 텍스트를 반환한다."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if self._provider == "ollama":
            payload: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
                "format": OLLAMA_OUTPUT_SCHEMA,
                "options": {
                    "temperature": ANALYSIS_TEMPERATURE,
                    "top_p": ANALYSIS_TOP_P,
                    "num_ctx": OLLAMA_CONTEXT_TOKENS,
                    "num_predict": OLLAMA_MAX_RESPONSE_TOKENS,
                },
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
            }
            if self._disable_thinking:
                payload["think"] = False
            endpoint = "api/chat"
        else:
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": ANALYSIS_TEMPERATURE,
                "top_p": ANALYSIS_TOP_P,
                "max_tokens": MAX_RESPONSE_TOKENS,
                "stream": False,
            }
            if self._disable_thinking:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            endpoint = "chat/completions"

        try:
            response = self._http_client.post(endpoint, json=payload)
        except httpx.TimeoutException as error:
            raise AiClientError(
                f"AI 응답이 {self._timeout_seconds:g}초 안에 오지 않았습니다. 모델: {self._model}",
                auto_retryable=True,
            ) from error
        except httpx.HTTPError as error:
            raise AiClientError(
                "AI 서버에 연결하지 못했습니다. 주소와 서버 실행 여부를 확인하세요. "
                f"원인: {type(error).__name__}",
                auto_retryable=not isinstance(
                    error, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)
                ),
            ) from error

        if not response.is_success:
            retryable = response.status_code == 429 or response.status_code >= 500
            try:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            except AiClientError as error:
                error.auto_retryable = retryable
                raise
            raise AiClientError(
                f"AI 서버가 오류를 반환했습니다. 상태 코드: {response.status_code}, "
                f"모델: {self._model}, 응답: {response.text[:ERROR_BODY_PREVIEW_CHARS]}",
                retryable=retryable,
                retry_after_seconds=retry_after,
                auto_retryable=retryable,
            )

        try:
            response_payload = response.json()
            if self._provider == "ollama":
                content = response_payload["message"]["content"]
            else:
                content = response_payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            if self._provider == "ollama":
                message = "AI 응답이 Ollama chat 형식이 아닙니다."
            else:
                message = "AI 응답이 OpenAI 호환 chat completions 형식이 아닙니다."
            raise AiClientError(f"{message} 모델: {self._model}") from error

        if not isinstance(content, str):
            raise AiClientError(f"AI 응답에 텍스트 내용이 없습니다. 모델: {self._model}")
        return content

    def close(self) -> None:
        self._http_client.close()


def _ollama_native_base_url(base_url: str) -> str:
    """OpenAI 호환 Ollama 주소에서 네이티브 API의 호스트 주소를 만든다."""
    normalized = base_url.rstrip("/")
    return normalized.removesuffix("/v1")


def _parse_retry_after(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        raise AiClientError(
            "AI 재시도 대기시간이 유효하지 않거나 상한 60초를 초과했습니다.",
            retryable=False,
        )
    return seconds
