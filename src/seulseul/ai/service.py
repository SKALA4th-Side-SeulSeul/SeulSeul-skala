"""공지 원문을 교육생이 바로 이해할 수 있는 요약으로 정제한다.

프롬프트와 모델 출력 정리 규칙을 담당하고, 실제 호출은 ai/client.py의 ChatClient에 맡긴다.
"""

import re

from seulseul.ai.client import AiClientError, ChatClient

# 긴 공지가 모델 입력 한도와 응답 시간을 넘지 않도록 앞부분만 보낸다.
MAX_NOTICE_INPUT_CHARS = 4000

SUMMARY_SYSTEM_PROMPT = """\
너는 교육생에게 Slack 공지를 정리해 주는 도우미다.
공지에 적힌 내용만 사용하고, 없는 내용은 추측하거나 만들지 않는다.
반드시 한국어로, 아래 형식 그대로 답한다. 형식 밖의 설명은 붙이지 않는다.

요약: 공지 핵심을 한 문장으로
할 일:
- 교육생이 해야 할 행동 (없으면 "- 없음")
마감: 공지에 적힌 기한 그대로 (없으면 "없음")"""

# 일부 로컬 모델(qwen 계열 등)은 답변 앞에 <think>…</think> 추론 과정을 붙인다.
REASONING_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
REASONING_START_PATTERN = re.compile(r"<think>", re.IGNORECASE)


class NoticeSummarizer:
    def __init__(self, client: ChatClient, max_input_chars: int = MAX_NOTICE_INPUT_CHARS) -> None:
        if max_input_chars < 1:
            raise ValueError(f"max_input_chars는 1 이상이어야 합니다. 전달된 값: {max_input_chars}")
        self._client = client
        self._max_input_chars = max_input_chars

    def summarize(self, notice_text: str) -> str:
        """공지 원문을 요약한다.

        AI 호출이 실패하거나 정리한 요약이 비어 있으면 AiClientError를 발생시킨다.
        """
        user_prompt = f"다음 공지를 정리해 줘.\n\n{notice_text[: self._max_input_chars]}"
        raw_output = self._client.complete(SUMMARY_SYSTEM_PROMPT, user_prompt)
        summary = remove_reasoning_blocks(raw_output)
        if not summary:
            raise AiClientError(
                "AI가 빈 요약을 반환했습니다. 모델이 추론 과정만 출력했는지 확인하세요."
            )
        return summary


def remove_reasoning_blocks(text: str) -> str:
    """<think> 추론 블록을 지운다. 응답 길이 제한으로 닫히지 않은 블록은 끝까지 지운다."""
    without_closed_blocks = REASONING_BLOCK_PATTERN.sub("", text)
    unclosed_block = REASONING_START_PATTERN.search(without_closed_blocks)
    if unclosed_block is not None:
        without_closed_blocks = without_closed_blocks[: unclosed_block.start()]
    return without_closed_blocks.strip()
