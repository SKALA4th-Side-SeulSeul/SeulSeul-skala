"""OpenAI 호환 chat completions API 클라이언트.

NVIDIA Build API와 Ollama가 같은 요청 형식(/v1/chat/completions)을 지원해
클라이언트 하나로 호출한다.
외부 AI 호출은 이 모듈에만 둔다(D-002). API 키는 로그와 오류 메시지에 넣지 않는다.
제공자 선택은 D-013을 따르고, 호출 파라미터와 오류 분류는 D-017을 따른다.
"""

from typing import Any, Protocol

import httpx

# 같은 공지에 대한 분석이 크게 흔들리지 않도록 낮은 온도를 쓴다.
ANALYSIS_TEMPERATURE = 0.2
ANALYSIS_TOP_P = 0.8
MAX_RESPONSE_TOKENS = 512
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
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.retry_count = retry_count


class ChatClient(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class OpenAICompatibleChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        disable_thinking: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._disable_thinking = disable_thinking
        # transport는 테스트에서 가짜 응답을 넣기 위해서만 사용한다.
        self._http_client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """시스템 지시와 사용자 입력을 보내고 모델의 답변 텍스트를 반환한다."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": ANALYSIS_TEMPERATURE,
            "top_p": ANALYSIS_TOP_P,
            "max_tokens": MAX_RESPONSE_TOKENS,
            "stream": False,
        }
        if self._disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        try:
            response = self._http_client.post("chat/completions", json=payload)
        except httpx.TimeoutException as error:
            raise AiClientError(
                f"AI 응답이 {self._timeout_seconds:g}초 안에 오지 않았습니다. 모델: {self._model}"
            ) from error
        except httpx.HTTPError as error:
            raise AiClientError(
                "AI 서버에 연결하지 못했습니다. 주소와 서버 실행 여부를 확인하세요. "
                f"원인: {type(error).__name__}"
            ) from error

        if not response.is_success:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise AiClientError(
                f"AI 서버가 오류를 반환했습니다. 상태 코드: {response.status_code}, "
                f"모델: {self._model}, 응답: {response.text[:ERROR_BODY_PREVIEW_CHARS]}",
                retryable=retryable,
                retry_after_seconds=_parse_retry_after(response.headers.get("Retry-After")),
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AiClientError(
                f"AI 응답이 OpenAI 호환 chat completions 형식이 아닙니다. 모델: {self._model}"
            ) from error

        if not isinstance(content, str):
            raise AiClientError(f"AI 응답에 텍스트 내용이 없습니다. 모델: {self._model}")
        return content

    def close(self) -> None:
        self._http_client.close()


def _parse_retry_after(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
